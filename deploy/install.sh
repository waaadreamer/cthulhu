#!/usr/bin/env bash
# Cthulhu v4 星系节点：一条命令安装 / 升级。
#
#   curl -fsSL <中心给出的 install.sh 地址> | bash -s -- <邀请>
#
# 用将要运行 agent 的普通用户执行，不需要 root，也不需要入站端口。
# 重跑同一条命令即升级：更新代码、保留 ~/.cthulhu/node.json 身份与 state/ 原生会话指针。
set -euo pipefail

INVITE=${1:-}
CTHULHU_HOME=${CTHULHU_HOME:-$HOME/.cthulhu}
REPO=${CTHULHU_REPO:-https://github.com/waaadreamer/cthulhu.git}
BRANCH=${CTHULHU_BRANCH:-v4}
APP="$CTHULHU_HOME/app"
VENV="$CTHULHU_HOME/venv"
LOG="$CTHULHU_HOME/node.log"
UNIT=cthulhu-node

say() { printf '\033[1;36m›\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$INVITE" || -f "$CTHULHU_HOME/node.json" ]] || die '缺少邀请：请复制中心网页「添加节点」给出的完整命令'
[[ $(uname -s) == Linux ]] || die '执行节点需要 Linux（用 /proc 核验原生进程）'
PY=${CTHULHU_PYTHON:-}
if [[ -z "$PY" ]]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY=$c; break; fi
  done
fi
[[ -n "$PY" ]] || die '需要 Python 3.11 或更新版本'
umask 077
mkdir -p "$CTHULHU_HOME"

# 1. 代码：本地源（测试/离线）或 git 分支。只覆盖 app/，不触碰 state/。
if [[ -n "${CTHULHU_SRC:-}" ]]; then
  say "使用本地代码 $CTHULHU_SRC"
  rm -rf "$APP.new" && mkdir -p "$APP.new"
  (cd "$CTHULHU_SRC" && tar --exclude=.git --exclude=.venv --exclude='.state*' --exclude=__pycache__ -cf - .) | tar -xf - -C "$APP.new"
  rm -rf "$APP" && mv "$APP.new" "$APP"
elif [[ -d "$APP/.git" ]]; then
  say "更新代码（$BRANCH）"
  git -C "$APP" fetch -q --depth 1 origin "$BRANCH" && git -C "$APP" reset -q --hard FETCH_HEAD
else
  command -v git >/dev/null || die '需要 git'
  say "下载代码（$BRANCH）"
  rm -rf "$APP" && git clone -q --depth 1 -b "$BRANCH" "$REPO" "$APP"
fi

# 2. 虚拟环境
if [[ ! -x "$VENV/bin/python" ]]; then say '创建虚拟环境'; "$PY" -m venv "$VENV"; fi
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade "$APP" \
  || die 'pip 安装失败（可设置 PIP_INDEX_URL 使用镜像后重试）'
CLI="$VENV/bin/cthulhu"

# 3. 加入中心（已加入时什么都不改）
if [[ -n "$INVITE" ]]; then
  CTHULHU_HOME="$CTHULHU_HOME" "$CLI" node join "$INVITE" --home "$CTHULHU_HOME"
fi

# 4. 本机能找到的原生后端；登录由用户自己在本机完成。
for b in codex claude; do
  if command -v "$b" >/dev/null; then say "发现 $b：$(command -v "$b")"; else say "未发现 $b（只会接收 demo 或其他后端任务）"; fi
done

# 5. 常驻：优先 systemd --user，否则 nohup。先停旧进程，确保同一状态目录只有一个节点。
run_cmd="$CLI node run --home $CTHULHU_HOME"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  unit_dir="$HOME/.config/systemd/user"; mkdir -p "$unit_dir"
  cat > "$unit_dir/$UNIT.service" <<UNIT
[Unit]
Description=Cthulhu v4 galaxy node
After=network-online.target

[Service]
Environment=PATH=$PATH
ExecStart=$run_cmd
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=20
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable -q "$UNIT"
  systemctl --user restart "$UNIT"
  command -v loginctl >/dev/null && loginctl enable-linger "$USER" 2>/dev/null || true
  manager="systemctl --user status $UNIT"
else
  if [[ -f "$CTHULHU_HOME/node.pid" ]] && kill -0 "$(cat "$CTHULHU_HOME/node.pid")" 2>/dev/null; then
    kill "$(cat "$CTHULHU_HOME/node.pid")"; for _ in $(seq 40); do kill -0 "$(cat "$CTHULHU_HOME/node.pid")" 2>/dev/null || break; sleep .5; done
  fi
  nohup $run_cmd >>"$LOG" 2>&1 &
  echo $! > "$CTHULHU_HOME/node.pid"
  manager="tail -f $LOG"
fi

# 6. 等到真正连上中心才算成功。
start=$(wc -c <"$LOG" 2>/dev/null || echo 0)
for _ in $(seq 60); do
  if tail -c +"$((start + 1))" "$LOG" 2>/dev/null | grep -q '已连接中心'; then
    say "节点已连接中心 ✓   日志：$LOG   管理：$manager"
    exit 0
  fi
  sleep 0.5
done
tail -n 20 "$LOG" >&2 || true
die "30 秒内未连上中心，请查看 $LOG"
