#!/usr/bin/env bash
# 一键复跑：功能断言（含手算案例）、分日回款/现金柜、20/300/1000 规模测试、
# CLI 子进程端到端、分日现场演示。
set -euo pipefail
cd "$(dirname "$0")"
echo "=== 1/5 功能测试（手算小例 + 六类规定场景 + 重开复读） ==="
python3 -m tests.test_service
echo
echo "=== 2/5 分日回款 + 现金柜（三种情形 + 循环/滞后共存 + 补款旧账） ==="
python3 -m tests.test_daily
echo
echo "=== 3/5 规模测试（20 模型 / 300 格 / 1000 引用 / 12 位分数） ==="
python3 -m tests.test_scale
echo
echo "=== 4/5 CLI 端到端（put-model/snapshot/compile/solve/query/explain/verify） ==="
python3 -m tests.demo_cli
echo
echo "=== 5/5 分日回款 / 现金柜现场演示（真实编译与落盘入口） ==="
python3 -m tests.demo_daily
