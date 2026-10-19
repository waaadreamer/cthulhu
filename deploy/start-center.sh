#!/usr/bin/env bash
# 本服务器的 v4 中心。经 nginx 8001 暴露：/v4/ → 8767，/v4/galaxy → 8770。
# 重复启动沿用同一状态目录与 Master 原生会话；换模型请换 --state。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python -m cthulhu_v4 center \
  --state .state --port 8767 --ws-port 8770 \
  --public-url https://47.115.202.248:8001/v4 \
  --backend codex --model gpt-6-astra --reasoning-effort medium
