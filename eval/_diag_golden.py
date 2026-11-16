"""临时诊断：手动执行 tc_l3_002 黄金 SQL"""
import json
import sys

sys.path.insert(0, "/evalsrc")
from eval.run_eval import get_exec_engine, run_golden

eng = get_exec_engine("postgresql+psycopg2://aix_db:1@127.0.0.1:5432/evaldb")
cases = [json.loads(l) for l in open("/evalsrc/eval/dataset/text2sql_eval_v1.jsonl") if l.strip()]
c = [x for x in cases if x["case_id"] == "tc_l3_002"][0]
try:
    cols, rows = run_golden(eng, c["expected"]["sql_alternatives"][0])
    print("cols:", cols)
    print("row0:", rows[0])
except Exception as e:
    import traceback
    traceback.print_exc()
