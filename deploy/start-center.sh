#!/usr/bin/env bash
# 使用原状态目录保留节点凭证；重复启动不重置已建立的真实 Codex session。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python -m cthulhu_v4 \
  --state .state-demo --port 8767 --ws-port 8770 \
  --public-origin https://47.115.202.248:8001 \
  --backend codex --model gpt-6-astra --reasoning-effort medium \
  --activate-demo-master --node alpha --node beta --node galaxy-01
