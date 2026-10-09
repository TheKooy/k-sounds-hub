#!/usr/bin/env bash
set -Eeuo pipefail

APP_ID="k-sounds-hub"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_ROOT="$DATA_HOME/$APP_ID"
APP_DIR="$APP_ROOT/app"
RETIRED_DIR="$APP_ROOT/retired"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
HUD_STATE_DIR="$CONFIG_HOME/audio-stack/hud_overlay"
REMOVE_CONFIG=0
PURGE_RETIRED=0

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -d "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"

usage() {
  cat <<'USAGE'
K-Sounds Hub user uninstall

Usage:
  ./uninstall.sh [--remove-config] [--purge-retired]

This removes only known user-installed K-Sounds Hub components.
It does not remove distro packages. Saved config and retired local backups are
preserved unless their explicit options are used.

Options:
  --remove-config   Also remove ~/.config/k-sounds-hub and HUD runtime state.
  --purge-retired   Also remove backups previously quarantined under retired/.
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --remove-config) REMOVE_CONFIG=1 ;;
    --purge-retired) PURGE_RETIRED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

stop_user_services() {
  local units=(
    ksound-soundboard-web.service
    k-sounds-soundboard-web.service
    k-sounds-hud-overlay.service
  )
  local unit

  if command -v systemctl >/dev/null 2>&1; then
    for unit in "${units[@]}"; do
      systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
    done
  fi

  for unit in "${units[@]}"; do
    rm -f -- "$SYSTEMD_USER_DIR/$unit"
  done

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
}

stop_runtime() {
  local stopper="$SOURCE_DIR/scripts/stop_all_ksound_runtime.sh"
  if [[ -x "$stopper" ]]; then
    "$stopper" >/dev/null 2>&1 || true
  elif [[ -x "$APP_DIR/scripts/stop_all_ksound_runtime.sh" ]]; then
    "$APP_DIR/scripts/stop_all_ksound_runtime.sh" >/dev/null 2>&1 || true
  fi
}

remove_known_files() {
  local bin_names=(
    k-sounds-hub
    k-sounds-hub-classic
    k-sounds-hub-glass
    ksound-hub
    ksound-hub-glass
    ksound-hub-v2
    ksounds-hub
    ksounds-hub-glass
    ksound-apply-zip
    ksound-soundboard-pair
    ksound-soundboard-play
    ksound-soundboard-stop-all
    ksound-soundboard-web
    ksound-v2-audio-keepalive
    ksound-v2-control
    ksound-v2-ensure-micro
    ksound-v2-restore-routes
    ksound-window-main-offset
    start_ksound_native_real_bg.sh
    start_ksound_v2_clean.sh
    start_audio_hud_overlay.sh
    audio-hud-overlay
  )
  local desktop_names=(
    k-sounds-hub.desktop
    k-sounds-hub-glass.desktop
    ksound-hub.desktop
    ksound-hub-glass.desktop
    ksound-hub-v2.desktop
    ksounds-hub.desktop
    ksounds-hub-glass.desktop
  )
  local name path

  for name in "${bin_names[@]}"; do
    rm -f -- "$BIN_DIR/$name"
  done

  for name in "${desktop_names[@]}"; do
    rm -f -- \
      "$APPS_DIR/$name" \
      "$AUTOSTART_DIR/$name" \
      "$DESKTOP_DIR/$name"
  done

  shopt -s nullglob
  for path in \
    "$AUTOSTART_DIR"/ksounds-hub.desktop.disabled-by-glass-* \
    "$AUTOSTART_DIR"/k-sounds-hub.desktop.disabled-by-glass-* \
    "$APPS_DIR"/net.local.ksound-v2-*.desktop
  do
    [[ -f "$path" || -L "$path" ]] && rm -f -- "$path"
  done
  shopt -u nullglob
}

stop_user_services
stop_runtime
remove_known_files

# Remove only the installed app. APP_ROOT may also contain retired backups
# created by migration patches and must therefore not be deleted wholesale.
rm -rf -- "$APP_DIR"

if (( REMOVE_CONFIG )); then
  rm -rf -- "$CONFIG_HOME/k-sounds-hub" "$HUD_STATE_DIR"
fi

if (( PURGE_RETIRED )); then
  rm -rf -- "$RETIRED_DIR"
fi

rmdir -- "$APP_ROOT" >/dev/null 2>&1 || true

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true

cat <<EOF_DONE
K-Sounds Hub user install removed.

Removed:
  $APP_DIR
  known K-Sounds wrappers, launchers, autostarts and user services

Config removed: $([[ "$REMOVE_CONFIG" == 1 ]] && echo yes || echo no)
Retired backups removed: $([[ "$PURGE_RETIRED" == 1 ]] && echo yes || echo no)
EOF_DONE

if [[ -d "$RETIRED_DIR" ]]; then
  echo "Retired backups preserved: $RETIRED_DIR"
fi
