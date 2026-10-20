#!/usr/bin/env bash
# 本服务器的 v4 中心。经 nginx 8001 暴露：/v4/ → 8767，/v4/galaxy → 8770。
# 重复启动沿用同一状态目录与 Master 原生会话；换模型请换 --state。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python -m cthulhu_v4 center \
  --state .state --port 8767 --ws-port 8770 \
  --public-url https://47.115.202.248:8001/v4 \
  --backend codex --model gpt-6-astra --reasoning-effort medium \
  --model-proxy http://127.0.0.1:7897 --git-proxy http://127.0.0.1:7897 \
  --feishu-cli /opt/cthulhu-feishu/node_modules/@larksuite/cli/bin/lark-cli \
  --feishu-owner ou_659dca076021f9a06fb3b9bc6ae56e49 --feishu-bot ou_8c7f5811726da050454b2c4ebd2b6784
