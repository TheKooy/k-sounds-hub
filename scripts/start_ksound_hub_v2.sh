#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"

export KSH_PROFILE_SUFFIX="V2"
export KSH_APP_NAME="K-Sounds Hub"
export KSH_ORG_NAME="K-Sounds Hub"
export KSH_ORG_DOMAIN="local.ksoundshub"
export KSH_CONFIG_DIR="${KSH_CONFIG_DIR:-$HOME/.config/k-sounds-hub}"
export KSH_IPC_SOCKET_PATH="${KSH_IPC_SOCKET_PATH:-/tmp/ksounds_hub_audio_$(id -u).sock}"
export KSH_HUD_STATE_DIR="${KSH_HUD_STATE_DIR:-$HOME/.config/audio-stack/hud_overlay}"

# The native Micro engine is the validated default. If its binary is absent or
# cannot start, the Python audio engine automatically falls back to the
# PipeWire loopback path. KSH_NATIVE_MIC=0 remains available for diagnostics.
export KSH_NATIVE_MIC="${KSH_NATIVE_MIC:-1}"
export KSH_MIC_METER_BOOST="${KSH_MIC_METER_BOOST:-4.0}"
export KSH_MIC_METER_FLOOR="${KSH_MIC_METER_FLOOR:-0.0015}"
export KSH_LEGACY_MIC_GAIN_PERCENT="${KSH_LEGACY_MIC_GAIN_PERCENT:-115}"

PRESERVE_AUDIO_ROUTING="${KSH_PRESERVE_AUDIO_ROUTING:-0}"
preserve_audio_routing() {
  case "${PRESERVE_AUDIO_ROUTING,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

RUNTIME_DIR="$KSH_CONFIG_DIR/runtime"
LOCK_FILE="$RUNTIME_DIR/app.lock"
IPC_SOCKET="$KSH_IPC_SOCKET_PATH"

cd "$REPO_DIR"
mkdir -p "$RUNTIME_DIR"

if [[ -x "$VENV_DIR/bin/python" ]]; then
  PYTHON_BIN="$VENV_DIR/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

export PYTHONPATH="$REPO_DIR/src"

FRONTEND_MODULE="${KSH_FRONTEND_MODULE:-ksound_hub.glass_app}"
case "$FRONTEND_MODULE" in
  ksound_hub.glass_app|ksound_hub.app) ;;
  *)
    echo "Unsupported K-Sounds frontend module: $FRONTEND_MODULE" >&2
    exit 2
    ;;
esac

is_real_instance_running() {
  pgrep -u "$(id -u)" -f 'python(3)? .* -m ksound_hub\.(glass_app|app)|ksound_hub\.(glass_app|app)' >/dev/null 2>&1
}

try_restore() {
  "$PYTHON_BIN" - <<'PY'
import json
import os
import socket

sock = os.environ["KSH_IPC_SOCKET_PATH"]
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.3)
    client.connect(sock)
    client.sendall((json.dumps({"command": "restore"}) + "\n").encode())
    client.close()
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
}

cleanup_stale_runtime_files() {
  rm -f "$IPC_SOCKET" || true
  rm -f "$LOCK_FILE" || true
}

start_soundboard_remote_service() {
  local start_remote="${KSH_START_SOUNDBOARD_REMOTE:-1}"
  case "${start_remote,,}" in
    0|false|no|off)
      return 0
      ;;
  esac

  # A cold K-Sounds launch must always reload the Remote source currently on
  # disk.  A legacy Remote may still answer /health on port 8765, and the
  # runtime's `start` action intentionally accepts any healthy responder.
  # `restart` atomically retires any old ksound_soundboard_web.py process and
  # spawns the Remote from this exact project tree.  The single-instance fast
  # path above exits before this function, so clicking the launcher again does
  # not bounce an already-current Remote.
  if ! "$PYTHON_BIN" -m ksound_hub.soundboard_remote_runtime \
    restart --project-root "$REPO_DIR" --quiet
  then
    echo "WARNING: Current Soundboard Remote did not start; audio and Glass will continue." >&2
    return 0
  fi
}

cleanup_managed_processes() {
  "$PYTHON_BIN" - <<'PY'
import os
import signal
import time
from pathlib import Path

TARGET_ROLES = {"eq_slot", "meter_probe", "native_meter_engine", "native_engine_shadow", "native_engine_bridge", "native_engine_real", "native_micro_engine", "v2_final_capture", "v2_final_render", "soundboard_remote_web", "hud_overlay_renderer", "glass_device_watch"}
uid = os.getuid()
proc_root = Path("/proc")
pids = []

for entry in proc_root.iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    if pid == os.getpid():
        continue

    status_path = entry / "status"
    environ_path = entry / "environ"
    try:
        status_text = status_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue

    real_uid = None
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                real_uid = int(parts[1])
            break
    if real_uid != uid:
        continue

    try:
        environ_raw = environ_path.read_bytes()
    except Exception:
        continue

    env = {}
    for chunk in environ_raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        env[key.decode("utf-8", errors="ignore")] = value.decode("utf-8", errors="ignore")

    if env.get("KSH_RUNTIME_ROLE") in TARGET_ROLES:
        pids.append(pid)

def cmdline_for(pid: int) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\\0", b" ").decode("utf-8", errors="replace")


COMMAND_PATTERNS = (
    "ksound_native_meter_engine",
    "ksound_native_micro_engine",
    "ksound_native_engine",
    "pipewire -c filter-chain.conf",
    "parec --device=soundboard.monitor --raw --format=float32le --rate=48000 --channels=2 --latency-msec=20",
    "pacat --playback --device=micro_bus --raw --format=float32le --rate=48000 --channels=2 --latency-msec=40 --process-time-msec=10",
)

for entry in proc_root.iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    if pid == os.getpid() or pid in pids:
        continue

    try:
        status_text = (entry / "status").read_text(encoding="utf-8", errors="replace")
    except Exception:
        continue

    real_uid = None
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                real_uid = int(parts[1])
            break
    if real_uid != uid:
        continue

    cmdline = cmdline_for(pid)
    if any(pattern in cmdline for pattern in COMMAND_PATTERNS):
        pids.append(pid)

for sig in (signal.SIGTERM, signal.SIGKILL):
    for pid in list(dict.fromkeys(pids)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    if sig == signal.SIGTERM and pids:
        time.sleep(1.0)
PY
}

# Fast path: if Glass already owns the IPC socket, restore/focus it and exit.
# Do this before Remote startup or runtime cleanup so a launcher click cannot
# restart a healthy K-Sounds session.
if try_restore; then
  exit 0
fi

# During the short startup window the process can exist before AudioIpcServer
# has bound its socket. Glass already watches this activation file, so nudge
# the existing instance instead of killing/restarting it.
if is_real_instance_running; then
  ACTIVATION_FILE="${XDG_CACHE_HOME:-$HOME/.cache}/k-sounds-hub/glass.activate"
  mkdir -p "$(dirname "$ACTIVATION_FILE")"
  touch "$ACTIVATION_FILE" 2>/dev/null || true
  exit 0
fi

if [[ -S "$IPC_SOCKET" ]] && ! is_real_instance_running; then
  cleanup_stale_runtime_files
fi

if preserve_audio_routing; then
  # UI/Core handoff: keep the existing PipeWire graph and K-Sounds audio
  # workers alive. Only stale UI-owned IPC/lock files are safe to clear here.
  cleanup_stale_runtime_files
else
  cleanup_managed_processes
  cleanup_stale_runtime_files

  if [[ -x "$HOME/.config/audio-stack/audio-setup.sh" ]]; then
    "$HOME/.config/audio-stack/audio-setup.sh" >/dev/null 2>&1 || true
  fi

  if [[ -x "$REPO_DIR/scripts/start_ksound_native_shadow_bridge.sh" ]]; then
    "$REPO_DIR/scripts/start_ksound_native_shadow_bridge.sh" >/dev/null 2>&1 || true
  fi
fi

# Let Qt select the native platform by default. An explicit override remains
# available for diagnostics without forcing Glass through XWayland.
if [[ -n "${KSH_QT_PLATFORM:-}" ]]; then
  export QT_QPA_PLATFORM="$KSH_QT_PLATFORM"
fi
if ! preserve_audio_routing; then
  start_soundboard_remote_service
fi

exec "$PYTHON_BIN" -m "$FRONTEND_MODULE"
