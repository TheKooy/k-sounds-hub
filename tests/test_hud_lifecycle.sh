#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/scripts/start_hud.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ksounds-hud-test.XXXXXX")"
TEST_CONFIG="$TEST_ROOT/config"
FAKE_HUD="$TEST_ROOT/audio-hud-overlay"
PID_FILE="$TEST_CONFIG/runtime/hud-overlay.pid"

run_hud() {
  env \
    KSH_CONFIG_DIR="$TEST_CONFIG" \
    KSH_HUD_BIN="$FAKE_HUD" \
    "$LAUNCHER" "$1"
}

cleanup() {
  run_hud stop >/dev/null 2>&1 || true
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

printf '%s\n' \
  '#!/usr/bin/env python3' \
  'import time' \
  'while True:' \
  '    time.sleep(1)' >"$FAKE_HUD"
chmod +x "$FAKE_HUD"

run_hud start
run_hud status
[[ -r "$PID_FILE" ]] || { echo "HUD lifecycle test: missing PID file" >&2; exit 1; }
read -r first_pid <"$PID_FILE"
kill -0 "$first_pid"

# Starting twice must keep a single renderer.
run_hud start
read -r duplicate_pid <"$PID_FILE"
[[ "$duplicate_pid" == "$first_pid" ]] || {
  echo "HUD lifecycle test: duplicate renderer was started" >&2
  exit 1
}

# A crashed renderer must be replaceable by the next watchdog start.
kill -KILL "$first_pid"
for _attempt in {1..40}; do
  ! kill -0 "$first_pid" 2>/dev/null && break
  sleep 0.05
done
! kill -0 "$first_pid" 2>/dev/null || { echo "HUD lifecycle test: crashed renderer did not exit" >&2; exit 1; }

run_hud start
run_hud status
read -r restarted_pid <"$PID_FILE"
[[ "$restarted_pid" != "$first_pid" ]] || {
  echo "HUD lifecycle test: renderer was not restarted" >&2
  exit 1
}
kill -0 "$restarted_pid"

# Disabling the setting calls this stop action and must terminate only this instance.
run_hud stop
if run_hud status; then
  echo "HUD lifecycle test: renderer still active after stop" >&2
  exit 1
fi
[[ ! -e "$PID_FILE" ]] || { echo "HUD lifecycle test: stale PID file" >&2; exit 1; }

echo "HUD lifecycle contract: single start, crash recovery and targeted stop passed"
