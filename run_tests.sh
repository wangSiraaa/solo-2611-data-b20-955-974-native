#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、报价曲线/预算核对、20/300/1000 规模、CLI 端到端。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/4 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/4 报价曲线与采购额度核对（四分类目录 + 题目手算三版本 + 十亿件） ==="
python3 -m tests.test_quoting
echo
echo "=== 3/4 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 4/4 CLI 端到端（put-model/snapshot/compile/solve/query/quote/budget/verify） ==="
python3 -m tests.demo_cli
