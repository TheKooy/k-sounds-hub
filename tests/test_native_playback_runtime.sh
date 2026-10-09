#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_IP05="$(mktemp -d)"
ENGINE_PID=""

cleanup() {
  if [[ -n "$ENGINE_PID" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
    kill -TERM "$ENGINE_PID" 2>/dev/null || true
    wait "$ENGINE_PID" 2>/dev/null || true
  fi
  if [[ "${KSH_KEEP_IP05_TEST_TMP:-0}" == "1" ]]; then
    echo "IP-05 test files kept at: $TMPDIR_IP05" >&2
  else
    rm -rf -- "$TMPDIR_IP05"
  fi
}
trap cleanup EXIT

command -v c++ >/dev/null 2>&1 || {
  echo "SKIP: c++ unavailable"
  exit 0
}

c++ \
  -std=c++20 -O2 -Wall -Wextra -Wpedantic \
  -I "$ROOT/native_engine/include" \
  "$ROOT/native_engine/src/main.cpp" \
  "$ROOT/native_engine/src/files.cpp" \
  "$ROOT/native_engine/src/realtime.cpp" \
  "$ROOT/native_engine/src/engine.cpp" \
  -o "$TMPDIR_IP05/ksound_native_engine"

mkdir -p "$TMPDIR_IP05/bin" "$TMPDIR_IP05/runtime"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'trap "exit 0" TERM INT' \
  'while :; do' \
  '  dd if=/dev/zero bs=7680 count=1 status=none || exit 0' \
  '  sleep 0.005' \
  'done' \
  > "$TMPDIR_IP05/bin/parec"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'counter="${KSH_TEST_PACAT_COUNTER:?}"' \
  'count=0' \
  '[[ ! -f "$counter" ]] || count="$(<"$counter")"' \
  'printf "%s\n" "$((count + 1))" > "$counter"' \
  'dd bs=7680 count=1 status=none of=/dev/null || true' \
  'exit 7' \
  > "$TMPDIR_IP05/bin/pacat"

chmod +x "$TMPDIR_IP05/bin/parec" "$TMPDIR_IP05/bin/pacat"

printf 'version\t2\nchannel\tall\t1\t0\t100\ttest-output\ttest-output\t\t100\n' \
  > "$TMPDIR_IP05/runtime/render_state.txt"
printf 'version\t1\nvolume\tall\t0\t100\n' \
  > "$TMPDIR_IP05/runtime/volume_state.txt"

PATH="$TMPDIR_IP05/bin:$PATH" \
KSH_TEST_PACAT_COUNTER="$TMPDIR_IP05/runtime/pacat-count" \
  "$TMPDIR_IP05/ksound_native_engine" \
    --state "$TMPDIR_IP05/runtime/render_state.txt" \
    --volume-state "$TMPDIR_IP05/runtime/volume_state.txt" \
    --levels "$TMPDIR_IP05/runtime/levels.json" \
    --log "$TMPDIR_IP05/runtime/engine.log" \
    --period-ms 20 \
    >"$TMPDIR_IP05/runtime/engine-console.log" 2>&1 &
ENGINE_PID="$!"

sleep 3.2
if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
  wait "$ENGINE_PID" || ENGINE_EXIT="$?"
  echo "FAIL: native engine exited early (${ENGINE_EXIT:-unknown})" >&2
  sed -n '1,160p' "$TMPDIR_IP05/runtime/engine-console.log" >&2 || true
  exit 1
fi

PACAT_COUNT="$(<"$TMPDIR_IP05/runtime/pacat-count")"
(( PACAT_COUNT >= 3 )) || {
  echo "FAIL: pacat was not restarted after broken pipes ($PACAT_COUNT)" >&2
  exit 1
}

kill -STOP "$ENGINE_PID"
sleep 0.3
kill -CONT "$ENGINE_PID"
sleep 0.3
kill -0 "$ENGINE_PID"
grep -q 'scheduler_resync_after_pause' "$TMPDIR_IP05/runtime/engine.log"

kill -TERM "$ENGINE_PID"
wait "$ENGINE_PID"
ENGINE_PID=""
sleep 0.2

if pgrep -f "$TMPDIR_IP05/bin/(parec|pacat)" >/dev/null 2>&1; then
  echo "FAIL: native playback child survived engine shutdown" >&2
  exit 1
fi

echo "native playback crash/resume lifecycle: passed"
