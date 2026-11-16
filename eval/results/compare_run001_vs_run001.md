# 评测对比：run001 vs run001

## 主指标卡
| 维度 | 基线 | 本轮 | Δ |
|---|---|---|---|
| D1 执行准确率 | 96.2% | 96.2% | +0.0pp |
| D2 SQL 有效性 | 94.9% | 94.9% | +0.0pp |
| D3 权限合规率 | 100.0% | 100.0% | +0.0pp |
| 整体通过率 | 95.0% | 95.0% | +0.0pp |
| D6 延迟 | — | P50=8.5s P95=25.4s | — |

## 分层看板（整体通过率）
| 层级 | 基线 | 本轮 | Δ |
|---|---|---|---|
| L1 | 100.0% | 100.0% | +0.0pp |
| L2 | 100.0% | 100.0% | +0.0pp |
| L3 | 66.7% | 66.7% | +0.0pp |
| L4 | 100.0% | 100.0% | +0.0pp |
| L5 | 100.0% | 100.0% | +0.0pp |

## 回归清单（上轮过 → 本轮挂，共 0 条）

## 本轮修复（上轮挂 → 本轮过，共 0 条）

## 失败归因分布（本轮）
- schema_misunderstand: 2

## 失败明细（本轮）
### tc_l3_002 (L3, admin)
- query: 每个员工的订单总金额占全体员工订单总额的百分比
- sql: `WITH "emp_order_total" AS (SELECT "e"."emp_id" AS "emp_id", "e"."emp_name" AS "emp_name", COALESCE(SUM("o"."amount"), 0) AS "order_total_amount" FROM "public"."emp" "e" LEFT JOIN "public"."orders" "o" ON "e"."emp_id" = "o"."emp_id" GROUP BY "e"."emp_id", "e"."emp_name"), "grand_total" AS (SELECT SUM`
- fail: result_mismatch: 列名不一致: got=['emp_name', 'order_amount_percent'], golden=['emp_name', 'pct']
### tc_l3_005 (L3, admin)
- query: 哪些员工的订单总金额高于全体员工的平均订单总金额？
- sql: `WITH "emp_order_total" AS (SELECT "o"."emp_id" AS "emp_id", SUM("o"."amount") AS "total_amount" FROM "public"."orders" "o" GROUP BY "o"."emp_id"), "avg_total" AS (SELECT AVG("total_amount") AS "avg_total_amount" FROM "emp_order_total") SELECT "e"."emp_id" AS "emp_id", "e"."emp_name" AS "emp_name", "`
- fail: result_mismatch: 列名不一致: got=['avg_total_amount', 'emp_id', 'emp_name', 'total_amount'], golden=['emp_name', 'total']