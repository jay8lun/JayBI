"""
Aix-DB Text2SQL 评测执行管线（run_eval.py）

两种模式：
- direct（推荐，在 aix-db 容器内运行）：直调 Text2SqlAgent 的 LangGraph，拿到最终
  filtered_sql / execution_result，绕过 HTTP 层，聚焦 SQL 质量与权限合规。
- api：调用 /llm/get_answer SSE 接口，覆盖端到端（含鉴权、SSE 协议）。

用法示例（容器内 direct 模式，3 次重复）：
    docker cp eval aix-db:/app/eval
    docker exec aix-db python /app/eval/run_eval.py \
        --mode direct --datasource-id 1 --exec-url "postgresql+psycopg2://user:pwd@host:5432/evaldb" \
        --run-id run001 --repeat 3

本地 api 模式：
    python eval/run_eval.py --mode api --api-base http://localhost:18088 \
        --token <JWT> --run-id run001

输出：eval/results/<run_id>/<case_id>__r<k>.json（每个 case 每次重复一个文件）
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根

from eval.judges import judge_case, attribute_failure, compare_result_sets  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eval")

EVAL_DIR = Path(__file__).resolve().parent
DATASET = EVAL_DIR / "dataset" / "text2sql_eval_v1.jsonl"

# user_role -> aix-db 内部 user_id 映射（可用 eval/users.json 覆盖）
DEFAULT_ROLE_USER_MAP = {"admin": 1, "analyst_ro": 2, "viewer_south": 3}


def load_cases(layers=None):
    cases = []
    with open(DATASET, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            if layers and c["layer"] not in layers:
                continue
            cases.append(c)
    return cases


def load_role_user_map():
    p = EVAL_DIR / "users.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return DEFAULT_ROLE_USER_MAP


# ---------------------------------------------------------------------------
# golden 结果集：在评测库上执行黄金 SQL
# ---------------------------------------------------------------------------

def get_exec_engine(exec_url):
    if not exec_url:
        return None
    from sqlalchemy import create_engine, text
    return create_engine(exec_url, pool_pre_ping=True)


def run_golden(engine, golden_sql):
    from sqlalchemy import text
    with engine.connect() as conn:
        result = conn.execute(text(golden_sql))
        cols = [c for c in result.keys()]
        rows = [dict(zip(cols, r)) for r in result.fetchall()]
    return cols, rows


# ---------------------------------------------------------------------------
# direct 模式：直调 LangGraph
# ---------------------------------------------------------------------------

async def run_case_direct(case, agent, role_user_map, engine):
    from agent.text2sql.state.agent_state import AgentState

    user_id = role_user_map.get(case["user_role"], 1)
    ds_id = case.get("datasource_id")
    t0 = time.time()
    outcome = {"success": False, "sql": None, "columns": None, "rows": None,
               "error": None, "latency_ms": 0.0, "refusal": False}
    try:
        graph = agent.create_graph(ds_id) if ds_id else agent.create_graph(None)
        state = await graph.ainvoke({
            "user_query": case["query"],
            "attempts": 0,
            "correct_attempts": 0,
            "datasource_id": ds_id,
            "user_id": user_id,
        })
        outcome["latency_ms"] = (time.time() - t0) * 1000
        final_sql = state.get("filtered_sql") or state.get("generated_sql") or ""
        outcome["sql"] = final_sql.strip() or None
        exec_res = state.get("execution_result")
        if exec_res and getattr(exec_res, "success", False):
            data = exec_res.data or []
            if data and isinstance(data[0], dict):
                outcome["columns"] = list(data[0].keys())
                outcome["rows"] = data
            outcome["success"] = True
        elif exec_res and getattr(exec_res, "error", None):
            outcome["error"] = exec_res.error
        elif state.get("error_message"):
            outcome["error"] = state["error_message"]
            outcome["refusal"] = True
    except Exception as e:  # noqa: BLE001
        outcome["latency_ms"] = (time.time() - t0) * 1000
        outcome["error"] = f"{type(e).__name__}: {e}"
    # 黄金结果
    expected = case.get("expected", {})
    if engine and expected.get("sql_alternatives"):
        try:
            outcome["golden_columns"], outcome["golden_rows"] = run_golden(
                engine, expected["sql_alternatives"][0]
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 黄金 SQL 执行失败: %s", case["case_id"], e)
    return outcome


# ---------------------------------------------------------------------------
# api 模式：SSE 端到端
# ---------------------------------------------------------------------------

_SQL_RE = re.compile(r"(SELECT|WITH)\b", re.I)


def extract_sql_from_events(events):
    """从 SSE t02/t04 事件里启发式提取最终 SQL。"""
    sql = None
    for ev in reversed(events):  # 从后往前，取最后一次出现的 SELECT 语句
        content = str(ev.get("content", ""))
        if _SQL_RE.search(content) and ";" in content:
            m = re.search(r"((?:WITH|SELECT)\b.+?);", content, re.S | re.I)
            if m:
                sql = m.group(1)
                break
    return sql


async def run_case_api(case, api_base, token, session):
    import aiohttp

    payload = {
        "query": case["query"],
        "qa_type": "DATABASE_QA",
        "datasource_id": case.get("datasource_id"),
        "chat_id": f"eval_{case['case_id']}",
        "uuid_str": f"{case['case_id']}_{int(time.time()*1000)}",
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    t0 = time.time()
    outcome = {"success": False, "sql": None, "columns": None, "rows": None,
               "error": None, "latency_ms": 0.0, "refusal": False, "events": []}
    try:
        async with session.post(f"{api_base}/llm/get_answer", json=payload,
                                headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            async for raw in resp.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                d = msg.get("data", {})
                dtype = msg.get("dataType")
                outcome["events"].append({"dataType": dtype, "content": d.get("content")})
                if dtype == "t09":
                    break
        outcome["latency_ms"] = (time.time() - t0) * 1000
        outcome["sql"] = extract_sql_from_events(outcome["events"])
        # 启发式：t04 里找 rows/columns
        for ev in outcome["events"]:
            if ev["dataType"] == "t04" and isinstance(ev["content"], dict):
                outcome["columns"] = ev["content"].get("columns")
                outcome["rows"] = ev["content"].get("data") or ev["content"].get("rows")
                outcome["success"] = True
                break
        if not outcome["success"]:
            texts = [str(e["content"]) for e in outcome["events"] if e["dataType"] == "t02"]
            joined = " ".join(texts)
            for kw in ("无法", "抱歉", "不支持", "权限", "错误", "失败"):
                if kw in joined:
                    outcome["error"] = joined[:300]
                    outcome["refusal"] = True
                    break
    except Exception as e:  # noqa: BLE001
        outcome["error"] = f"{type(e).__name__}: {e}"
        outcome["latency_ms"] = (time.time() - t0) * 1000
    return outcome


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["direct", "api"], default="direct")
    p.add_argument("--run-id", required=True)
    p.add_argument("--repeat", type=int, default=3, help="每 case 重复次数（抗 LLM 随机性）")
    p.add_argument("--layers", nargs="*", default=None, help="只跑指定层级，如 L1 L4")
    p.add_argument("--datasource-id", type=int, default=None, help="覆盖 case 里的 datasource_id")
    p.add_argument("--exec-url", default=os.getenv("EVAL_DATABASE_URL"),
                   help="评测库 SQLAlchemy URL（执行黄金 SQL 用）")
    p.add_argument("--api-base", default="http://localhost:8088")
    p.add_argument("--token", default=os.getenv("EVAL_TOKEN"))
    args = p.parse_args()

    cases = load_cases(args.layers)
    role_user_map = load_role_user_map()
    out_dir = EVAL_DIR / "results" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = get_exec_engine(args.exec_url) if args.mode == "direct" else None

    agent = None
    if args.mode == "direct":
        from agent.text2sql.text2_sql_agent import Text2SqlAgent
        agent = Text2SqlAgent()

    total = len(cases) * args.repeat
    done = 0
    summary_pass = 0

    for rep in range(1, args.repeat + 1):
        for case in cases:
            if args.mode == "direct":
                if case["layer"] == "L6":
                    logger.info("[%s] L6 多轮 case 跳过（direct 模式不支持会话历史）", case["case_id"])
                    continue
                c = dict(case)
                if args.datasource_id:
                    c["datasource_id"] = args.datasource_id
                outcome = await run_case_direct(c, agent, role_user_map, engine)
            else:
                async with aiohttp.ClientSession() as session:
                    outcome = await run_case_api(case, args.api_base, args.token, session)

            verdict = judge_case(case, outcome)
            verdict["attribution"] = attribute_failure(verdict, outcome)
            record = {
                "case_id": case["case_id"], "layer": case["layer"],
                "repeat": rep, "user_role": case["user_role"],
                "query": case["query"], "verdict": verdict, "outcome": {
                    k: outcome.get(k) for k in
                    ("success", "sql", "columns", "rows", "error", "latency_ms", "refusal")
                },
            }
            (out_dir / f"{case['case_id']}__r{rep}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            done += 1
            summary_pass += bool(verdict["overall"])
            logger.info("[%d/%d] %s -> %s %s", done, total, case["case_id"],
                        "PASS" if verdict["overall"] else "FAIL",
                        verdict.get("failure_reason") or verdict.get("attribution") or "")

    logger.info("完成：%d/%d 通过（%.1f%%）", summary_pass, total,
                100.0 * summary_pass / max(total, 1))
    logger.info("生成对比报告: python eval/report.py --base <旧run_id> --target %s", args.run_id)


if __name__ == "__main__":
    asyncio.run(main())
