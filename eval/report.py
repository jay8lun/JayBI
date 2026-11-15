"""
评测对比报告（report.py）
对比两个 run 的结果，产出 markdown：
- 主指标卡（各维度通过率 + Δ）
- 分层热力表（层级 x 通过率）
- 回归清单（上轮过、本轮挂 —— 最重要）
- 归因分布

用法：python eval/report.py --base run001 --target run002
输出：eval/results/compare_run001_vs_run002.md
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
RESULTS = EVAL_DIR / "results"

D1 = "d1_exec_accuracy"
D2 = "d2_sql_valid"
D3 = "d3_permission"


def load_run(run_id):
    """返回 {case_id: [record, ...]}（多次重复聚合成 per-case 多数票）。"""
    run_dir = RESULTS / run_id
    by_case = defaultdict(list)
    for f in sorted(run_dir.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        by_case[rec["case_id"]].append(rec)
    return by_case


def aggregate(by_case):
    """多数票：一个 case 在 ≥50% 的重复里通过即算通过。返回 {case_id: record}。"""
    agg = {}
    for cid, recs in by_case.items():
        passes = sum(1 for r in recs if r["verdict"].get("overall"))
        verdict = dict(recs[0]["verdict"])
        verdict["overall"] = passes * 2 > len(recs)
        verdict["pass_rate"] = passes / len(recs) if recs else 0
        if not verdict["overall"]:
            fails = [r for r in recs if not r["verdict"].get("overall")]
            verdict["failure_reason"] = fails[0]["verdict"].get("failure_reason", "")
            verdict["attribution"] = fails[0]["verdict"].get("attribution", "other")
        agg[cid] = {**recs[0], "verdict": verdict}
    return agg


def rate(records, key):
    """某维度的通过率（None=不适用，跳过分母）。"""
    total = sum(1 for r in records if r["verdict"].get(key) is not None)
    if total == 0:
        return None
    ok = sum(1 for r in records if r["verdict"].get(key))
    return ok / total


def pct(v):
    return "—" if v is None else f"{v*100:.1f}%"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--target", required=True)
    args = p.parse_args()

    base = aggregate(load_run(args.base))
    target = aggregate(load_run(args.target))
    common = sorted(set(base) & set(target))
    layers = sorted({r["layer"] for r in target.values()})

    lines = []
    lines.append(f"# 评测对比：{args.target} vs {args.base}")
    lines.append("")
    lines.append("## 主指标卡")
    lines.append("| 维度 | 基线 | 本轮 | Δ |")
    lines.append("|---|---|---|---|")
    for key, name in ((D1, "D1 执行准确率"), (D2, "D2 SQL 有效性"), (D3, "D3 权限合规率"),
                      ("overall", "整体通过率")):
        rb, rt = rate([base[c] for c in common], key), rate([target[c] for c in common], key)
        delta = "" if (rb is None or rt is None) else f"{(rt-rb)*100:+.1f}pp"
        mark = " ⚠" if (rb is not None and rt is not None and rt < rb) else ""
        lines.append(f"| {name} | {pct(rb)} | {pct(rt)} | {delta}{mark} |")
    if target.values():
        lat = [r["outcome"]["latency_ms"] for r in target.values()
               if r["outcome"].get("latency_ms")]
        if lat:
            lat.sort()
            p50 = lat[len(lat)//2] / 1000
            p95 = lat[int(len(lat)*0.95)] / 1000
            lines.append(f"| D6 延迟 | — | P50={p50:.1f}s P95={p95:.1f}s | — |")

    lines.append("")
    lines.append("## 分层看板（整体通过率）")
    lines.append("| 层级 | 基线 | 本轮 | Δ |")
    lines.append("|---|---|---|---|")
    for layer in layers:
        cb = [base[c] for c in common if base[c]["layer"] == layer]
        ct = [target[c] for c in common if target[c]["layer"] == layer]
        rb, rt = rate(cb, "overall"), rate(ct, "overall")
        delta = "" if (rb is None or rt is None) else f"{(rt-rb)*100:+.1f}pp"
        lines.append(f"| {layer} | {pct(rb)} | {pct(rt)} | {delta} |")

    # 回归清单（最重要）
    regressions = [c for c in common if base[c]["verdict"]["overall"]
                   and not target[c]["verdict"]["overall"]]
    fixed = [c for c in common if not base[c]["verdict"]["overall"]
             and target[c]["verdict"]["overall"]]
    lines.append("")
    lines.append(f"## 回归清单（上轮过 → 本轮挂，共 {len(regressions)} 条）")
    if len(regressions) > len(fixed):
        lines.append(f"**⚠ 回归数({len(regressions)}) > 修复数({len(fixed)})，按门禁禁止合入！**")
    for c in regressions:
        v = target[c]["verdict"]
        lines.append(f"- {c} [{target[c]['layer']}] 归因: {v.get('attribution','?')}"
                     f" — {v.get('failure_reason','')[:120]}")
    lines.append("")
    lines.append(f"## 本轮修复（上轮挂 → 本轮过，共 {len(fixed)} 条）")
    for c in fixed:
        lines.append(f"- {c} [{target[c]['layer']}]")

    lines.append("")
    lines.append("## 失败归因分布（本轮）")
    attr_count = defaultdict(int)
    for c in target.values():
        if not c["verdict"]["overall"]:
            attr_count[c["verdict"].get("attribution", "other")] += 1
    for tag, n in sorted(attr_count.items(), key=lambda x: -x[1]):
        lines.append(f"- {tag}: {n}")

    lines.append("")
    lines.append("## 失败明细（本轮）")
    for c in sorted(target):
        r = target[c]
        if r["verdict"]["overall"]:
            continue
        lines.append(f"### {c} ({r['layer']}, {r['user_role']})")
        lines.append(f"- query: {r['query']}")
        lines.append(f"- sql: `{(r['outcome'].get('sql') or '')[:300]}`")
        lines.append(f"- fail: {r['verdict'].get('failure_reason','')}")
        if r["outcome"].get("error"):
            lines.append(f"- error: {r['outcome']['error'][:200]}")

    out = RESULTS / f"compare_{args.base}_vs_{args.target}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已生成: {out}")
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()
