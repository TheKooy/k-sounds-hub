#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
export KSH_CONFIG_DIR="${KSH_CONFIG_DIR:-$HOME/.config/k-sounds-hub}"
export KSH_IPC_SOCKET_PATH="${KSH_IPC_SOCKET_PATH:-/tmp/ksounds_hub_audio_$(id -u).sock}"
RUNTIME_DIR="$KSH_CONFIG_DIR/runtime"
LOG_FILE="$RUNTIME_DIR/ui-handoff.log"
mkdir -p "$RUNTIME_DIR"

if [[ -x "$VENV_DIR/bin/python" ]]; then
  PYTHON_BIN="$VENV_DIR/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

wait_pid=""
if [[ "${1:-}" == "--after-pid" ]]; then
  wait_pid="${2:-}"
  [[ "$wait_pid" =~ ^[0-9]+$ ]] || { echo "Invalid --after-pid" >&2; exit 2; }
  shift 2
fi

find_ui_pids() {
  pgrep -u "$(id -u)" -f 'python(3)? .* -m ksound_hub\.(glass_app|app)|ksound_hub\.(glass_app|app)' || true
}

send_preserve_restart() {
  "$PYTHON_BIN" - <<'PY'
import json, os, socket
sock = os.environ["KSH_IPC_SOCKET_PATH"]
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.8)
client.connect(sock)
client.sendall((json.dumps({"command": "restart-ui-preserve-audio"}) + "\n").encode())
client.close()
PY
}

had_ui=0
if [[ -n "$wait_pid" ]]; then
  had_ui=1
else
  mapfile -t before < <(find_ui_pids)
  if (( ${#before[@]} )); then
    had_ui=1
    send_preserve_restart
    wait_pid="${before[0]}"
  fi
fi

if (( had_ui == 0 )); then
  exec "$REPO_DIR/scripts/start_ksound_hub_v2.sh"
fi

# Give the old UI time to autosave and detach while leaving audio alive.
for _ in $(seq 1 100); do
  if [[ -n "$wait_pid" ]] && kill -0 "$wait_pid" 2>/dev/null; then
    sleep 0.05
    continue
  fi
  if [[ -z "$(find_ui_pids)" ]]; then
    break
  fi
  sleep 0.05
done

if [[ -n "$(find_ui_pids)" ]]; then
  echo "K-Sounds UI did not exit cleanly; preserving the current audio session and aborting relaunch." >&2
  exit 1
fi

export KSH_PRESERVE_AUDIO_ROUTING=1
nohup "$REPO_DIR/scripts/start_ksound_hub_v2.sh" >>"$LOG_FILE" 2>&1 </dev/null &
disown || true
printf 'K-Sounds UI handoff requested; audio routing kept alive.\n'
