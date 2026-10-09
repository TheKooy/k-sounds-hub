#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${1:-$HOME/k-sound-hub-v2}"
if (($#)); then
  shift
fi
INSTALL_DESKTOP_ENTRY=1
for arg in "$@"; do
  case "$arg" in
    --no-desktop) INSTALL_DESKTOP_ENTRY=0 ;;
    *) echo "Unknown migration option: $arg" >&2; exit 2 ;;
  esac
done

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
MIGRATION_STAMP="${KSH_MIGRATION_STAMP:-$(date +%Y%m%d-%H%M%S)}"
RETIRED_DIR="$DATA_HOME/k-sounds-hub/retired/integration-$MIGRATION_STAMP"
CANONICAL_WRAPPER="$BIN_DIR/k-sounds-hub"
CLASSIC_WRAPPER="$BIN_DIR/k-sounds-hub-classic"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -d "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"
KNOWN_DESKTOP_NAMES=(
  k-sounds-hub.desktop
  k-sounds-hub-glass.desktop
  ksound-hub.desktop
  ksound-hub-glass.desktop
  ksound-hub-v2.desktop
  ksounds-hub.desktop
  ksounds-hub-glass.desktop
)

[[ -x "$REPO_DIR/scripts/start_ksound_hub_v2.sh" ]] || {
  echo "K-Sounds launcher not found: $REPO_DIR/scripts/start_ksound_hub_v2.sh" >&2
  exit 1
}

retire_file() {
  local source="$1"
  local relative destination
  [[ -f "$source" || -L "$source" ]] || return 0

  if [[ "$source" == "$HOME/"* ]]; then
    relative="${source#"$HOME/"}"
  else
    relative="external/$(basename "$source")"
  fi
  destination="$RETIRED_DIR/$relative"
  mkdir -p "$(dirname "$destination")"

  if [[ -e "$destination" || -L "$destination" ]]; then
    local index=2
    while [[ -e "$destination.$index" || -L "$destination.$index" ]]; do
      ((index += 1))
    done
    destination="$destination.$index"
  fi
  mv -- "$source" "$destination"
}

desktop_entry_is_active() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  grep -Eiq '^[[:space:]]*Type[[:space:]]*=[[:space:]]*Application[[:space:]]*$' "$path" \
    || return 1
  grep -Eiq '^[[:space:]]*Exec[[:space:]]*=[[:space:]]*.+$' "$path" \
    || return 1
  ! grep -Eiq '^[[:space:]]*Hidden[[:space:]]*=[[:space:]]*true[[:space:]]*$' "$path" \
    || return 1
  ! grep -Eiq '^[[:space:]]*X-GNOME-Autostart-enabled[[:space:]]*=[[:space:]]*false[[:space:]]*$' "$path" \
    || return 1
}

existing_autostart_is_active() {
  local name
  for name in "${KNOWN_DESKTOP_NAMES[@]}"; do
    desktop_entry_is_active "$AUTOSTART_DIR/$name" && return 0
  done
  return 1
}

disable_old_remote_services() {
  local units=(
    ksound-soundboard-web.service
    k-sounds-soundboard-web.service
  )
  local unit

  if command -v systemctl >/dev/null 2>&1; then
    for unit in "${units[@]}"; do
      systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
    done
  fi

  for unit in "${units[@]}"; do
    retire_file "$SYSTEMD_USER_DIR/$unit"
  done

  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
}

retire_stale_launchers() {
  local name path
  retire_file "$BIN_DIR/ksound-soundboard-web"
  retire_file "$APPS_DIR/ksounds-hub.desktop"
  retire_file "$APPS_DIR/ksounds-hub-glass.desktop"
  retire_file "$APPS_DIR/k-sounds-hub-glass.desktop"
  retire_file "$DESKTOP_DIR/ksounds-hub.desktop"
  retire_file "$DESKTOP_DIR/ksounds-hub-glass.desktop"
  retire_file "$DESKTOP_DIR/k-sounds-hub-glass.desktop"

  for name in "${KNOWN_DESKTOP_NAMES[@]}"; do
    retire_file "$AUTOSTART_DIR/$name"
  done

  shopt -s nullglob
  for path in \
    "$AUTOSTART_DIR"/ksounds-hub.desktop.disabled-by-glass-* \
    "$AUTOSTART_DIR"/k-sounds-hub.desktop.disabled-by-glass-*
  do
    retire_file "$path"
  done
  shopt -u nullglob
}

install_wrappers() {
  local alias
  mkdir -p "$BIN_DIR"

  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail'
    printf 'exec %q "$@"\n' "$REPO_DIR/scripts/start_ksound_hub_v2.sh"
  } > "$CANONICAL_WRAPPER"
  chmod +x "$CANONICAL_WRAPPER"

  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail'
    printf 'exec env KSH_FRONTEND_MODULE=ksound_hub.app %q "$@"\n' \
      "$REPO_DIR/scripts/start_ksound_hub_v2.sh"
  } > "$CLASSIC_WRAPPER"
  chmod +x "$CLASSIC_WRAPPER"

  for alias in \
    ksound-hub-v2 \
    ksounds-hub \
    ksounds-hub-glass \
    k-sounds-hub-glass
  do
    {
      printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail'
      printf 'exec %q "$@"\n' "$CANONICAL_WRAPPER"
    } > "$BIN_DIR/$alias"
    chmod +x "$BIN_DIR/$alias"
  done
}

install_desktop_entry() {
  local desktop_path="$APPS_DIR/k-sounds-hub.desktop"
  local icon_path="$REPO_DIR/src/ksound_hub/assets/app_icon.png"
  local alias_path
  mkdir -p "$APPS_DIR"
  cat > "$desktop_path" <<EOF_DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=K-Sounds Hub
Comment=PipeWire-first modular Linux audio hub
Exec=$CANONICAL_WRAPPER
Icon=$icon_path
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
StartupWMClass=ksounds-hub
X-GNOME-UsesNotifications=true
EOF_DESKTOP
  chmod +x "$desktop_path"

  for alias_path in \
    "$APPS_DIR/ksounds-hub.desktop" \
    "$APPS_DIR/ksounds-hub-glass.desktop"
  do
    cat > "$alias_path" <<EOF_ALIAS
[Desktop Entry]
Type=Application
Version=1.0
Name=K-Sounds Hub
Comment=Compatibility launcher for existing KDE favorites
Exec=$CANONICAL_WRAPPER
Icon=$icon_path
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
StartupWMClass=ksounds-hub
NoDisplay=true
X-GNOME-UsesNotifications=true
EOF_ALIAS
    chmod +x "$alias_path"
  done
}

install_preserved_autostart() {
  (( PRESERVE_AUTOSTART )) || return 0
  local autostart_path="$AUTOSTART_DIR/k-sounds-hub.desktop"
  local icon_path="$REPO_DIR/src/ksound_hub/assets/app_icon.png"
  mkdir -p "$AUTOSTART_DIR"
  cat > "$autostart_path" <<EOF_AUTOSTART
[Desktop Entry]
Type=Application
Version=1.0
Name=K-Sounds Hub
Comment=Start the K-Sounds Hub Glass frontend
Exec=$CANONICAL_WRAPPER
TryExec=$CANONICAL_WRAPPER
Icon=$icon_path
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
EOF_AUTOSTART
  chmod +x "$autostart_path"
}

PRESERVE_AUTOSTART=0
existing_autostart_is_active && PRESERVE_AUTOSTART=1

disable_old_remote_services
retire_stale_launchers
install_wrappers
install_preserved_autostart
if (( INSTALL_DESKTOP_ENTRY )); then
  install_desktop_entry
fi

command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null 2>&1 \
  && kbuildsycoca6 >/dev/null 2>&1 || true

echo "K-Sounds user integration migrated to the canonical Glass launcher."
echo "Autostart preserved: $([[ "$PRESERVE_AUTOSTART" == 1 ]] && echo yes || echo no)"
if [[ -d "$RETIRED_DIR" ]]; then
  echo "Retired integration files preserved in: $RETIRED_DIR"
fi
