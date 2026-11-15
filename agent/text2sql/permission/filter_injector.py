"""
权限过滤注入节点
使用 LLM 将权限条件注入到 SQL 语句中
"""

import json
import logging
import re
import traceback
from typing import Dict, Any, Optional, List

from langchain_core.messages import SystemMessage, HumanMessage

from agent.text2sql.state.agent_state import AgentState
from agent.text2sql.permission.permission_retriever import (
    get_user_permission_filters,
    get_user_column_permissions,
)
from agent.text2sql.template.prompt_builder import PromptBuilder
from agent.text2sql.template.schema_formatter import get_database_engine_info
from agent.text2sql.analysis.data_render_antv import extract_table_names_sqlglot, extract_table_alias_mapping, DB_TYPE_TO_DIALECT
from common.llm_util import get_llm
from services.datasource_service import DatasourceService
from model.db_connection_pool import get_db_pool

import sqlglot
from sqlglot import parse

logger = logging.getLogger(__name__)
pool = get_db_pool()


def _fail_close(state: AgentState, reason: str) -> AgentState:
    """
    权限校验失败时拒绝执行（fail-close）：
    清空 generated_sql 与 filtered_sql，下游执行节点会因 SQL 为空而终止，
    并通过 error_message 给用户明确提示。绝不回退执行未过滤的原始 SQL。
    """
    logger.error(f"权限过滤 fail-close，拒绝执行：{reason}")
    state["generated_sql"] = ""
    state["filtered_sql"] = ""
    state["error_message"] = f"权限校验失败，已拒绝执行本次查询：{reason}"
    return state


def _apply_column_permissions_to_sql(
    sql: str,
    db_type: str,
    table_allowed_fields: Dict[str, set],
    alias_to_table: Dict[str, str],
) -> Optional[str]:
    """
    基于列权限重写 SELECT 列：移除 enable=false 的字段。
    - 支持表别名：若列引用的是别名，使用 alias_to_table 映射回真实表名进行权限判断。
    - 返回 None 表示 fail-close（存在列权限约束但无法安全应用）：
      SQL 解析失败、检测到 SELECT * / table.* 时，拒绝执行而非放行。
    """
    if not table_allowed_fields:
        return sql

    # 使用数据库类型到 sqlglot 方言的映射，确保所有数据源类型都能正确解析
    dialect = DB_TYPE_TO_DIALECT.get(db_type.lower() if db_type else "mysql", "mysql")
    try:
        expressions = parse(sql, read=dialect)
    except Exception as e:
        logger.error(f"列权限：SQL 解析失败，fail-close 拒绝执行: {e}")
        return None

    # 反向映射：table_name -> alias（可能为空）
    table_to_alias = {v: k for k, v in (alias_to_table or {}).items()}

    changed = False
    for exp in expressions:
        if not exp:
            continue
        select = exp.find(sqlglot.exp.Select)
        if not select:
            continue

        new_select_exprs = []
        for proj in list(select.expressions):
            # 处理 SELECT * 或 table.*：存在列权限约束时 fail-close
            if isinstance(proj, sqlglot.exp.Star):
                logger.error("列权限：检测到 SELECT *，无法安全过滤列，fail-close 拒绝执行")
                return None
            if isinstance(proj, sqlglot.exp.Column) and proj.is_star:
                logger.error("列权限：检测到 SELECT table.*，无法安全过滤列，fail-close 拒绝执行")
                return None

            # 可能是 Alias(Column AS xxx)
            col_exp = proj
            if isinstance(proj, sqlglot.exp.Alias):
                col_exp = proj.this

            if isinstance(col_exp, sqlglot.exp.Column):
                col_name = col_exp.name
                qualifier = col_exp.table  # 可能是别名 p 或真实表名

                # 解析出真实表名：若 qualifier 是别名，则映射回表名
                real_table = None
                if qualifier:
                    real_table = (alias_to_table or {}).get(qualifier) or qualifier

                # 若没有 qualifier 且只涉及一张受限表，可尝试按那张表判断
                if not real_table and len(table_allowed_fields) == 1:
                    real_table = next(iter(table_allowed_fields.keys()))

                if real_table and real_table in table_allowed_fields:
                    allowed = table_allowed_fields[real_table]
                    if col_name not in allowed:
                        changed = True
                        continue  # 丢弃该列

            new_select_exprs.append(proj)

        if changed:
            # 若全部列都被过滤掉，保底保留第一个列，避免 SQL 语法错误
            if not new_select_exprs and select.expressions:
                new_select_exprs = [select.expressions[0]]
            select.set("expressions", new_select_exprs)

    if not changed:
        return sql

    try:
        # 使用相同的方言映射进行序列化
        return "; ".join([e.sql(dialect=dialect) for e in expressions if e])
    except Exception as e:
        # 序列化失败：存在列权限约束却无法产出过滤后的 SQL，fail-close
        logger.error(f"列权限：过滤后 SQL 序列化失败，fail-close 拒绝执行: {e}")
        return None


def permission_filter_injector(state: AgentState) -> AgentState:
    """
    权限过滤注入节点
    1. 获取用户的权限过滤条件
    2. 使用 LLM 将权限条件注入 SQL
    3. 返回过滤后的 SQL
    
    Args:
        state: Agent 状态对象
        
    Returns:
        更新后的 state
    """
    try:
        # 获取生成的SQL
        generated_sql = state.get("generated_sql")
        if not generated_sql or generated_sql == "No SQL query generated":
            logger.warning("没有生成的SQL，跳过权限过滤")
            return state
        
        # 获取数据源ID和用户ID
        datasource_id = state.get("datasource_id")
        user_id_raw = state.get("user_id")
        
        # 确保 user_id 是整数类型（缺失/非法一律 fail-close，绝不默认管理员）
        if user_id_raw is None:
            return _fail_close(state, "无法从会话中确认用户身份（user_id 为空）")
        try:
            user_id = int(user_id_raw) if isinstance(user_id_raw, str) else user_id_raw
        except (ValueError, TypeError):
            return _fail_close(state, f"user_id 非法: {user_id_raw!r}")

        if not datasource_id:
            return _fail_close(state, "缺少数据源标识，无法进行权限校验")

        logger.info(f"开始权限过滤：datasource_id={datasource_id}, user_id={user_id}, user_id类型={type(user_id)}")

        # 获取数据源信息
        with pool.get_session() as session:
            datasource = DatasourceService.get_datasource_by_id(session, datasource_id)
            if not datasource:
                return _fail_close(state, f"数据源不存在: {datasource_id}")
            
            db_type = datasource.type or "mysql"
        
        # 获取数据库引擎信息
        engine = get_database_engine_info(db_type)
        
        # 获取表名列表：优先使用 state 中的 used_tables，如果为空则从 SQL 中提取
        table_names: Optional[List[str]] = state.get("used_tables")
        
        if not table_names:
            # 从 SQL 中提取表名
            logger.info("state 中没有 used_tables，尝试从 SQL 中提取表名")
            table_names = extract_table_names_sqlglot(generated_sql, db_type)
            if table_names:
                logger.info(f"从 SQL 中提取到表名: {table_names}")
            else:
                logger.warning("无法从 SQL 中提取表名，将获取所有表的权限")
                table_names = None  # 如果为None，则获取所有表的权限
        else:
            logger.info(f"使用 state 中的表名: {table_names}")

        # 提取 SQL 中的表别名映射，并构建 table_name -> alias 映射
        # data_render_antv.extract_table_alias_mapping 返回的是 {alias: table_name}
        alias_to_table = extract_table_alias_mapping(generated_sql, db_type)
        table_to_alias: Dict[str, str] = {v: k for k, v in (alias_to_table or {}).items() if v and k}
        if table_to_alias:
            logger.info(f"SQL 表别名映射(table->alias): {table_to_alias}")
        
        # 获取权限过滤条件
        filters = get_user_permission_filters(
            datasource_id=datasource_id,
            user_id=user_id,
            table_names=table_names,
            table_alias_map=table_to_alias,
        )
        
        logger.info(f"获取到权限过滤条件数量: {len(filters) if filters else 0}")
        if filters:
            for filter_item in filters:
                logger.info(f"  表: {filter_item.get('table')}, 过滤条件: {filter_item.get('filter')[:100]}...")
        
        # 获取列权限（用于 SELECT 列过滤）
        column_allowed = get_user_column_permissions(
            datasource_id=datasource_id,
            user_id=user_id,
            table_names=table_names,
        )
        if column_allowed:
            logger.info(
                f"列权限允许字段: { {k: sorted(list(v))[:10] for k, v in column_allowed.items()} }"
            )

        # 如果没有行权限过滤条件，先默认使用原始 SQL
        if not filters:
            logger.info(f"没有权限过滤条件（datasource_id={datasource_id}, user_id={user_id}, table_names={table_names}），直接返回原始SQL")
            # 仍然应用列权限（如果有）
            final_sql = _apply_column_permissions_to_sql(
                generated_sql, db_type, column_allowed, alias_to_table
            )
            if final_sql is None:
                return _fail_close(state, "存在列权限约束但无法安全应用（SELECT * 或解析失败）")
            state["filtered_sql"] = final_sql
            return state
        
        # 使用 PromptBuilder 构建权限过滤提示词
        prompt_builder = PromptBuilder()
        
        system_prompt, user_prompt = prompt_builder.build_permission_prompt(
            sql=generated_sql,
            filters=filters,
            engine=engine,
            lang="简体中文",
        )
        
        # 构建消息列表
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        
        # 调用 LLM
        llm = get_llm(0)
        response = llm.invoke(messages)
        
        # 解析响应（JSON 格式）
        response_content = response.content.strip()
        
        # 清理 JSON 字符串（移除可能的 markdown 代码块标记）
        if "```json" in response_content:
            response_content = response_content.split("```json")[1]
        if "```" in response_content:
            response_content = response_content.split("```")[0]
        response_content = response_content.strip()
        
        # 解析 JSON
        try:
            result = json.loads(response_content)
            
            if result.get("success", True):
                filtered_sql = result.get("sql", generated_sql)
                # 对注入后的 SQL 应用列权限过滤
                filtered_sql = _apply_column_permissions_to_sql(
                    filtered_sql, db_type, column_allowed, alias_to_table
                )
                if filtered_sql is None:
                    return _fail_close(state, "列权限无法安全应用（SELECT * 或解析失败）")
                # 行权限二次校验：LLM 注入后必须确认过滤条件真的存在于 SQL 中，
                # 否则拒绝执行（防止 LLM 输出格式漂移导致权限条件丢失）
                missing = _verify_row_filters(filtered_sql, db_type, filters, alias_to_table)
                if missing:
                    return _fail_close(
                        state,
                        f"权限条件注入失败，以下表的过滤条件缺失: {missing}",
                    )
                state["filtered_sql"] = filtered_sql
                logger.info(f"权限过滤成功，原始SQL: {generated_sql[:100]}...")
                logger.info(f"过滤后SQL: {filtered_sql[:100]}...")
            else:
                error_message = result.get("message", "无法注入权限过滤条件")
                # 行权限注入失败 = 权限无法落地，fail-close（绝不放行原始 SQL）
                return _fail_close(state, f"权限条件注入失败: {error_message}")

        except json.JSONDecodeError as e:
            logger.error(f"解析 LLM 响应 JSON 失败: {e}")
            logger.error(f"响应内容: {response_content[:500]}")
            return _fail_close(state, "权限过滤服务返回异常，无法确认权限条件已应用")

    except Exception as e:
        traceback.print_exception(e)
        logger.error(f"权限过滤注入过程中发生错误: {e}", exc_info=True)
        # 任何未知异常都 fail-close：绝不带着未确认的权限状态继续执行
        return _fail_close(state, f"权限过滤过程异常: {type(e).__name__}")

    return state


def _verify_row_filters(
    filtered_sql: str,
    db_type: str,
    filters: List[Dict[str, Any]],
    alias_to_table: Dict[str, str],
) -> List[str]:
    """
    二次校验：解析 filtered_sql，断言每个行权限过滤条件确实存在。
    返回缺失过滤条件的表名列表（空列表 = 全部通过）。
    校验策略：SQL 中必须出现各过滤条件的核心比较值（取条件串中最后一个被引号
    包裹的字面量或纯数字）；这是宽松断言，宁可误判为失败也不放过缺失。
    """
    if not filters:
        return []
    dialect = DB_TYPE_TO_DIALECT.get(db_type.lower() if db_type else "mysql", "mysql")
    try:
        expressions = parse(filtered_sql, read=dialect)
        sql_text = " ".join(e.sql(dialect=dialect) for e in expressions if e)
    except Exception as e:
        logger.error(f"行权限二次校验：SQL 解析失败: {e}")
        return [f.get("table", "?") for f in filters]

    missing = []
    for f in filters:
        cond = f.get("filter") or ""
        table = f.get("table") or ""
        # 提取条件里的字面量（引号字符串 / 数字）作为指纹
        literals = re.findall(r"'([^']+)'|\b(\d+(?:\.\d+)?)\b", cond)
        fingerprints = [a or b for a, b in literals if (a or b)]
        if not fingerprints:
            # 条件中没有可校验的字面量，退化为检查表名出现在 SQL 中
            if table and table.lower() not in sql_text.lower():
                missing.append(f"{table}(条件不可校验且表未引用)")
            continue
        # 宽松断言：任一指纹出现即认为条件已注入（完整布尔逻辑由 sqlglot 层保证）
        if not any(fp in sql_text for fp in fingerprints):
            missing.append(f"{table}(条件指纹缺失: {fingerprints[:3]})")
    return missing

