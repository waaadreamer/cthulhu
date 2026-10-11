#!/usr/bin/env bash
# One-command galaxy install for a cluster node. No root required.
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/cthulhu/main/deploy/install-galaxy.sh | bash -s -- <blob>
#
# <blob> comes from `python -m cthulhu provision` on the temple. It carries the
# galaxy id, its secret token and the temple websocket URL.
#
# Environment overrides:
#   CTHULHU_HOME   install directory            (default ~/.cthulhu)
#   CTHULHU_REPO   git URL                      (default github.com/waaadreamer/cthulhu)
#   CTHULHU_TUNNEL ssh target for a tunnel to the temple, e.g. root@47.115.202.248
#                  (sets up autossh-style reconnecting `ssh -N -L 7500:127.0.0.1:7500`)
#   CODEX_BIN      existing codex binary         (default: download @openai/codex via npm, else skip)
set -euo pipefail

BLOB="${1:?usage: install-galaxy.sh <provision-blob>}"
HOME_DIR="${CTHULHU_HOME:-$HOME/.cthulhu}"
REPO="${CTHULHU_REPO:-https://github.com/waaadreamer/cthulhu.git}"
say() { printf '\033[1;35m[cthulhu]\033[0m %s\n' "$*"; }

mkdir -p "$HOME_DIR" && cd "$HOME_DIR"

# 1. code
if [ -d src/.git ]; then
  say "updating code"; git -C src pull -q --ff-only
else
  say "cloning $REPO"; git clone -q --depth 1 "$REPO" src
fi

# 2. python >= 3.11 venv (uv if present, else system python)
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then PY="$c"; break; fi
done
if [ -z "$PY" ] && command -v uv >/dev/null; then
  say "no python>=3.11 found, using uv"; uv venv -q -p 3.12 venv
elif [ -z "$PY" ]; then
  echo "need python>=3.11 (or uv) on PATH" >&2; exit 1
elif [ ! -x venv/bin/python ]; then
  say "creating venv with $PY"
  "$PY" -m venv venv 2>/dev/null || { "$PY" -m venv --without-pip venv && curl -fsSL https://bootstrap.pypa.io/get-pip.py | venv/bin/python - -q; }
fi
say "installing python deps"
if command -v uv >/dev/null; then uv pip install -q -p venv/bin/python -e src; else venv/bin/python -m pip install -q -e src; fi

# 3. codex binary
CODEX="${CODEX_BIN:-}"
if [ -z "$CODEX" ] && command -v codex >/dev/null; then CODEX="$(command -v codex)"; fi
if [ -z "$CODEX" ] && command -v npm >/dev/null; then
  say "installing codex via npm (local prefix)"
  npm install -q --prefix "$HOME_DIR/codex" @openai/codex >/dev/null && CODEX="$HOME_DIR/codex/node_modules/.bin/codex"
fi
if [ -z "$CODEX" ]; then
  say "WARNING: no codex binary; set CODEX_BIN or copy one from the temple (/opt/cthulhu/codex-native) and rerun"
  CODEX="codex"
fi

# 4. config
say "writing config"
mkdir -p state
umask 077
"$HOME_DIR/venv/bin/python" - "$BLOB" "$HOME_DIR" "$CODEX" <<'PY'
import base64, json, sys
blob, home, codex = sys.argv[1:]
g = json.loads(base64.b64decode(blob))
g.update({"state": f"{home}/state", "codex": codex, "cwd": g.get("cwd") or f"{home}/work"})
g.setdefault("runtimes", {})
open(f"{home}/galaxy.json", "w").write(json.dumps(g, indent=2))
print(f"  galaxy {g['id']} -> {g['temple_ws']}")
PY
mkdir -p work
umask 022

# 5. launcher: tunnel (optional) + galaxy, restarted forever
cat > run.sh <<EOF
#!/usr/bin/env bash
cd "$HOME_DIR"
export PYTHONPATH="$HOME_DIR/src"
[ -f "$HOME_DIR/env" ] && set -a && . "$HOME_DIR/env" && set +a
if [ -n "${CTHULHU_TUNNEL:-}" ]; then
  ( while true; do ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \\
      -L 7500:127.0.0.1:7500 ${CTHULHU_TUNNEL:-} ; sleep 5; done ) &
  sleep 3
fi
while true; do
  "$HOME_DIR/venv/bin/python" -m cthulhu galaxy --config "$HOME_DIR/galaxy.json" >> "$HOME_DIR/galaxy.log" 2>&1
  sleep 5
done
EOF
chmod +x run.sh

# 6. keep it running: systemd --user if available, else nohup
if systemctl --user status >/dev/null 2>&1; then
  mkdir -p ~/.config/systemd/user
  cat > ~/.config/systemd/user/cthulhu-galaxy.service <<EOF
[Unit]
Description=Cthulhu galaxy
[Service]
ExecStart=$HOME_DIR/run.sh
Restart=always
KillMode=control-group
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now cthulhu-galaxy >/dev/null
  loginctl enable-linger "$USER" 2>/dev/null || true
  say "running as systemd --user service cthulhu-galaxy"
else
  pkill -f "$HOME_DIR/run.sh" 2>/dev/null || true
  nohup setsid "$HOME_DIR/run.sh" >/dev/null 2>&1 < /dev/null &
  say "running in background (no systemd --user); log: $HOME_DIR/galaxy.log"
fi

say "done. Put API credentials (e.g. ANTHROPIC_*) in $HOME_DIR/env if this galaxy runs Claude agents."
say "Codex login: run '$CODEX login' once as this user if it is not logged in."
