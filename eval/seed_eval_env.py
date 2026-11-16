"""
评测环境播种脚本（在 aix-db 容器内运行）

一次性创建：
1. eval 演示数据源（指向容器内 PG 的 evaldb）+ 表/字段元数据同步
2. 两个非管理员用户：analyst_ro / viewer_south
3. 数据源授权（t_datasource_auth）
4. 行权限（orders.region 枚举过滤）+ 列权限（emp.salary / orders.cost_price 禁用）
5. 权限规则（t_ds_rules 关联用户与权限）

用法：docker exec aix-db python /app/eval/seed_eval_env.py
幂等：全部按 name 查重，可重复执行。
输出：JSON 摘要（datasource_id / 用户 id 映射），并写入 /app/eval/users.json
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 项目根探测：容器内为 /aix-db；兼容 EVAL_PROJECT_ROOT 环境变量
_PROJECT_ROOT = os.getenv("EVAL_PROJECT_ROOT", "/aix-db")
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed")

from model.db_connection_pool import get_db_pool
from model.db_models import TUser, TDsPermission, TDsRules
from model.datasource_models import Datasource, DatasourceAuth, DatasourceField, DatasourceTable
from services.datasource_service import DatasourceService

EVAL_CONFIG = {
    "host": "127.0.0.1",
    "port": 5432,
    "username": "aix_db",
    "password": "1",
    "database": "evaldb",
    # 注意：get_tables/get_fields 的 PG 分支用 dbSchema 查 schema，缺省会误用 database 名
    "dbSchema": "public",
}
EVAL_TABLES = ["dept", "emp", "orders", "sales_target"]
DS_NAME = "eval_demo"


def get_or_create_datasource(session):
    ds = session.query(Datasource).filter(Datasource.name == DS_NAME).first()
    if ds:
        # 已存在的数据源若缺 dbSchema（旧配置），补齐后重加密
        from common.datasource_util import DatasourceConfigUtil

        try:
            cfg = DatasourceConfigUtil.decrypt_config(ds.configuration)
            if isinstance(cfg, dict) and not cfg.get("dbSchema"):
                cfg["dbSchema"] = EVAL_CONFIG["dbSchema"]
                ds.configuration = DatasourceConfigUtil.encrypt_config(cfg)
                session.commit()
                logger.info("已为存量数据源补齐 dbSchema=public")
        except Exception as e:
            logger.warning(f"检查存量配置失败: {e}")
        logger.info(f"数据源已存在: id={ds.id}")
        return ds
    ds = DatasourceService.create_datasource(
        session,
        {
            "name": DS_NAME,
            "description": "Text2SQL 评测专用演示库（固定种子数据）",
            "type": "pg",
            "type_name": "PostgreSQL",
            "configuration": EVAL_CONFIG,
        },
        user_id=1,
    )
    logger.info(f"数据源已创建: id={ds.id}")
    return ds


def sync_metadata(ds_id):
    with get_db_pool().get_session() as s:
        has_tables = s.query(DatasourceTable).filter(DatasourceTable.ds_id == ds_id).count() > 0
        has_fields = s.query(DatasourceField).filter(DatasourceField.ds_id == ds_id).count() > 0
    if not (has_tables and has_fields):
        with get_db_pool().get_session() as s:
            ok = DatasourceService.sync_tables(
                s, ds_id, [{"table_name": t} for t in EVAL_TABLES], is_select_all=True
            )
        logger.info(f"表/字段元数据同步: {'成功' if ok else '失败'}")
    else:
        logger.info("表/字段元数据已就绪，跳过同步")


def get_or_create_user(session, username):
    user = session.query(TUser).filter(TUser.userName == username).first()
    if user:
        return user
    admin = session.query(TUser).filter(TUser.role == "admin").first()
    user = TUser(
        userName=username,
        password=admin.password if admin else "x",
        mobile=None,
        role="user",
        createTime=datetime.now(),
    )
    session.add(user)
    session.commit()
    logger.info(f"用户已创建: {username} id={user.id}")
    return user


def get_field_id(session, ds_id, table_name, field_name):
    table = (
        session.query(DatasourceTable)
        .filter(DatasourceTable.ds_id == ds_id, DatasourceTable.table_name == table_name)
        .first()
    )
    field = (
        session.query(DatasourceField)
        .filter(DatasourceField.ds_id == ds_id, DatasourceField.table_id == table.id,
                DatasourceField.field_name == field_name)
        .first()
    )
    return field.id


def get_or_create_permission(session, name, ds_id, table_id, ptype, expression_tree=None, permissions=None):
    perm = session.query(TDsPermission).filter(TDsPermission.name == name).first()
    if perm:
        return perm
    perm = TDsPermission(
        name=name, type=ptype, ds_id=ds_id, table_id=table_id,
        expression_tree=json.dumps(expression_tree) if expression_tree else None,
        permissions=json.dumps(permissions) if permissions else None,
        enable=True,
    )
    session.add(perm)
    session.commit()
    logger.info(f"权限已创建: {name} id={perm.id}")
    return perm


def column_permission_payload(session, ds_id, table_name, disabled_fields):
    """生成列权限 permissions JSON：全字段默认 enable=true，disabled_fields 为 false。"""
    table = (
        session.query(DatasourceTable)
        .filter(DatasourceTable.ds_id == ds_id, DatasourceTable.table_name == table_name)
        .first()
    )
    fields = (
        session.query(DatasourceField)
        .filter(DatasourceField.ds_id == ds_id, DatasourceField.table_id == table.id)
        .all()
    )
    return [
        {"field_name": f.field_name, "enable": f.field_name not in disabled_fields}
        for f in fields
    ]


def get_or_create_rule(session, name, permission_ids, user_ids):
    rule = session.query(TDsRules).filter(TDsRules.name == name).first()
    if rule:
        return rule
    rule = TDsRules(
        name=name,
        permission_list=json.dumps(permission_ids),
        user_list=json.dumps(user_ids),
        enable=True,
    )
    session.add(rule)
    session.commit()
    logger.info(f"规则已创建: {name} id={rule.id}")
    return rule


def _fix_sequences(session):
    """显式插入过数据的表，自增序列可能落后，统一校准。"""
    from sqlalchemy import text

    for seq, table in (("t_user_id_seq", "t_user"), ("t_datasource_id_seq", "t_datasource")):
        session.execute(
            text(f"SELECT setval('{seq}', COALESCE((SELECT max(id) FROM {table}), 1))")
        )
    session.commit()


def main():
    with get_db_pool().get_session() as session:
        _fix_sequences(session)
        ds = get_or_create_datasource(session)
        ds_id = ds.id

    sync_metadata(ds_id)

    with get_db_pool().get_session() as session:
        analyst = get_or_create_user(session, "analyst_ro")
        viewer = get_or_create_user(session, "viewer_south")

        # 数据源授权
        for u in (analyst, viewer):
            exists = (
                session.query(DatasourceAuth)
                .filter(DatasourceAuth.datasource_id == ds_id, DatasourceAuth.user_id == u.id)
                .first()
            )
            if not exists:
                session.add(DatasourceAuth(datasource_id=ds_id, user_id=u.id, enable=True))
                session.commit()
                logger.info(f"已授权: {u.userName} -> 数据源 {ds_id}")

        orders_table = (
            session.query(DatasourceTable)
            .filter(DatasourceTable.ds_id == ds_id, DatasourceTable.table_name == "orders")
            .first()
        )
        emp_table = (
            session.query(DatasourceTable)
            .filter(DatasourceTable.ds_id == ds_id, DatasourceTable.table_name == "emp")
            .first()
        )

        # 行权限：analyst_ro 只看华东 / viewer_south 只看华南（orders.region 枚举）
        region_field_id = get_field_id(session, ds_id, "orders", "region")
        perm_row_east = get_or_create_permission(
            session, "eval_row_analyst_east", ds_id, orders_table.id, "row",
            expression_tree={"logic": "AND", "items": [
                {"type": "item", "field_id": region_field_id,
                 "filter_type": "enum", "enum_value": ["华东"]},
            ]},
        )
        perm_row_south = get_or_create_permission(
            session, "eval_row_viewer_south", ds_id, orders_table.id, "row",
            expression_tree={"logic": "AND", "items": [
                {"type": "item", "field_id": region_field_id,
                 "filter_type": "enum", "enum_value": ["华南"]},
            ]},
        )

        # 列权限：analyst_ro 禁用 emp.salary / orders.cost_price
        emp_cols = column_permission_payload(session, ds_id, "emp", {"salary"})
        orders_cols = column_permission_payload(session, ds_id, "orders", {"cost_price"})
        perm_col_emp = get_or_create_permission(
            session, "eval_col_emp_analyst", ds_id, emp_table.id, "column",
            permissions=emp_cols,
        )
        perm_col_orders = get_or_create_permission(
            session, "eval_col_orders_analyst", ds_id, orders_table.id, "column",
            permissions=orders_cols,
        )

        # 规则关联
        get_or_create_rule(session, "eval_rule_analyst",
                           [perm_row_east.id, perm_col_emp.id, perm_col_orders.id],
                           [analyst.id])
        get_or_create_rule(session, "eval_rule_viewer_south",
                           [perm_row_south.id], [viewer.id])

        result = {
            "datasource_id": ds_id,
            "admin_id": 1,
            "analyst_ro_id": analyst.id,
            "viewer_south_id": viewer.id,
            "permission_ids": {
                "row_east": perm_row_east.id, "row_south": perm_row_south.id,
                "col_emp": perm_col_emp.id, "col_orders": perm_col_orders.id,
            },
        }

    users_path = Path(_PROJECT_ROOT) / "eval" / "users.json"
    users_path.write_text(
        json.dumps({
            "admin": result["admin_id"],
            "analyst_ro": result["analyst_ro_id"],
            "viewer_south": result["viewer_south_id"],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"users.json 已写入: {users_path}")


if __name__ == "__main__":
    main()
