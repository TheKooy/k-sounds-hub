#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "===== REPO ====="
pwd
echo

echo "===== CORE COMMANDS ====="
for cmd in python3 pactl parec pw-cat ffmpeg cmake c++; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf '%-10s %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf '%-10s %s\n' "$cmd" "missing"
  fi
done
echo

echo "===== PIPEWIRE SERVICES ====="
systemctl --user --no-pager --full --lines=0 status pipewire.service pipewire-pulse.service wireplumber.service 2>/dev/null \
  | sed -n '/Loaded:/p;/Active:/p' || true
echo

echo "===== PYTHON MODULES ====="
PYTHON_BIN="python"
if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_DIR/.venv/bin/python"
fi

"$PYTHON_BIN" - <<'PY'
mods = ["PySide6", "numpy"]
for mod in mods:
    try:
        __import__(mod)
        print(f"{mod}: OK")
    except Exception as exc:
        print(f"{mod}: MISSING ({exc.__class__.__name__}: {exc})")
PY

echo
echo "===== RUNTIME COMPONENTS ====="
components=(
  "native playback|$REPO_DIR/native_engine/build/ksound_native_engine"
  "native Micro|$REPO_DIR/native_engine/build/ksound_native_micro_engine"
  "native meters|$REPO_DIR/native_engine/build/ksound_native_meter_engine"
  "Wayland HUD|$REPO_DIR/tools/hud_overlay/build/audio-hud-overlay"
)
for component in "${components[@]}"; do
  label="${component%%|*}"
  path="${component#*|}"
  if [[ -x "$path" ]]; then
    printf '%-18s OK (%s)\n' "$label" "$path"
  else
    printf '%-18s missing/optional (%s)\n' "$label" "$path"
  fi
done
