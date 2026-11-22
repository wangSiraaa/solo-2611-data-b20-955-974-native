#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、六类规定场景、重开、单向换挡实况册/计量发行、
# 规模测试、CLI 子进程端到端。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/4 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/4 单向换挡实况册 + 精确计量发行（72 项断言，含题面八情景、单调 DP 与区间内核） ==="
python3 -m tests.test_livebook
echo
echo "=== 3/4 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 4/4 CLI 端到端（budget 全套 + 实况册 lb-* 子命令） ==="
python3 -m tests.demo_cli
python3 -m tests.demo_livebook_cli
