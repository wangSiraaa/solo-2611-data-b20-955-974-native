#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、代拨去向册子系统、20/300/1000 规模、CLI 端到端。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/4 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/4 代拨去向册/点名册/拨款单（手算 12 场景 + 受困循环 + 稳定单号） ==="
python3 -m tests.test_allocation
echo
echo "=== 3/4 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 4/4 CLI 端到端（原预算 + 代拨用途变更两条链路） ==="
python3 -m tests.demo_cli
python3 -m tests.demo_allocation_cli
