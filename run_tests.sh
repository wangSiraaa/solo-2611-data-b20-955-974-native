#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、票面/询证断言、20/300/1000 规模测试、CLI 端到端。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/4 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/4 票面发行/校版/辨错/询证/回签状态机（题面全部手算例） ==="
python3 -m tests.test_tickets
echo
echo "=== 3/4 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 4/4 CLI 端到端（put-model/.../verify + 票版发行与带回签的询证签账） ==="
python3 -m tests.demo_cli
python3 -m tests.demo_ticket_cli
