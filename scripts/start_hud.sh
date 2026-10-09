#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${KSH_CONFIG_DIR:-$HOME/.config/k-sounds-hub}/runtime"
HUD_LOG="$RUNTIME_DIR/hud-overlay.log"
HUD_PID_FILE="$RUNTIME_DIR/hud-overlay.pid"
HUD_LOCK_FILE="$RUNTIME_DIR/hud-overlay.lock"
ACTION="${1:-start}"

HUD_BIN="${KSH_HUD_BIN:-}"
if [[ -z "$HUD_BIN" ]]; then
  for candidate in \
    "$REPO_DIR/tools/hud_overlay/build/audio-hud-overlay" \
    "$REPO_DIR/bin/audio-hud-overlay" \
    "$HOME/.local/bin/audio-hud-overlay"
  do
    if [[ -x "$candidate" ]]; then
      HUD_BIN="$candidate"
      break
    fi
  done
fi

mkdir -p "$RUNTIME_DIR"
if ! command -v flock >/dev/null 2>&1; then
  [[ "$ACTION" == "status" ]] && exit 1
  exit 0
fi

pid_is_alive() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

pid_matches_instance_if_inspectable() {
  local pid="$1"
  local environ_path="/proc/$pid/environ"
  [[ -e "$environ_path" ]] || return 0

  local env_lines
  env_lines="$({ tr '\0' '\n' <"$environ_path"; } 2>/dev/null)" || return 0
  [[ -n "$env_lines" ]] || return 0
  grep -Fxq 'KSH_RUNTIME_ROLE=hud_overlay_renderer' <<<"$env_lines" || return 1
  grep -Fxq "KSH_HUD_INSTANCE=$HUD_PID_FILE" <<<"$env_lines"
}

hud_lock_is_held() {
  ! flock -n "$HUD_LOCK_FILE" true 2>/dev/null
}

write_pid_file() {
  local pid="$1"
  local tmp="$HUD_PID_FILE.tmp.$$"
  printf '%s\n' "$pid" >"$tmp"
  mv -f -- "$tmp" "$HUD_PID_FILE"
}

read_hud_pid() {
  local pid=""
  if [[ -r "$HUD_PID_FILE" ]]; then
    read -r pid <"$HUD_PID_FILE" || true
    if pid_is_alive "$pid" && hud_lock_is_held && pid_matches_instance_if_inspectable "$pid"; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  if ! hud_lock_is_held; then
    rm -f -- "$HUD_PID_FILE"
  fi
  return 1
}

start_hud() {
  local existing_pid=""
  existing_pid="$(read_hud_pid || true)"
  [[ -z "$existing_pid" ]] || return 0
  hud_lock_is_held && return 0

  # The HUD is optional. A missing binary must never block Glass or audio.
  [[ -n "$HUD_BIN" && -x "$HUD_BIN" ]] || return 0

  nohup bash -c '
    lock_file="$1"
    hud_bin="$2"
    instance="$3"
    working_dir="$4"
    exec 9>"$lock_file"
    flock -n 9 || exit 0
    cd "$working_dir"
    exec env \
      KSH_RUNTIME_ROLE=hud_overlay_renderer \
      KSH_HUD_INSTANCE="$instance" \
      KSH_HUD_BIN="$hud_bin" \
      "$hud_bin"
  ' k-sounds-hud-supervisor \
    "$HUD_LOCK_FILE" \
    "$HUD_BIN" \
    "$HUD_PID_FILE" \
    "$(dirname "$HUD_BIN")" \
    >>"$HUD_LOG" 2>&1 </dev/null &
  local pid=$!

  local attempt
  for attempt in {1..20}; do
    if pid_is_alive "$pid" && hud_lock_is_held; then
      write_pid_file "$pid"
      return 0
    fi
    sleep 0.05
  done

  rm -f -- "$HUD_PID_FILE"
  return 1
}

stop_hud() {
  local pid=""
  pid="$(read_hud_pid || true)"
  if [[ -z "$pid" ]]; then
    if ! hud_lock_is_held; then
      rm -f -- "$HUD_PID_FILE"
    fi
    return 0
  fi

  kill -TERM "$pid" 2>/dev/null || true
  local attempt
  for attempt in {1..20}; do
    if ! pid_is_alive "$pid" || ! hud_lock_is_held; then
      rm -f -- "$HUD_PID_FILE"
      return 0
    fi
    sleep 0.05
  done

  kill -KILL "$pid" 2>/dev/null || true
  rm -f -- "$HUD_PID_FILE"
}

case "$ACTION" in
  start)
    start_hud
    ;;
  stop)
    stop_hud
    ;;
  status)
    read_hud_pid >/dev/null
    ;;
  *)
    printf 'Usage: %s [start|stop|status]\n' "$0" >&2
    exit 2
    ;;
esac
