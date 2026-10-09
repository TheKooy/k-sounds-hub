#!/usr/bin/env bash
set -Eeuo pipefail

APP_ID="k-sounds-hub"
APP_NAME="K-Sounds Hub"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_ROOT="$DATA_HOME/$APP_ID"
FINAL_APP_DIR="$APP_ROOT/app"
STAGING_APP_DIR="$APP_ROOT/.app-installing.$$"
PREVIOUS_APP_DIR="$APP_ROOT/.app-previous.$$"
APP_DIR="$FINAL_APP_DIR"
VENV_DIR="$APP_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
WRAPPER_PATH="$BIN_DIR/k-sounds-hub"
CLASSIC_WRAPPER_PATH="$BIN_DIR/k-sounds-hub-classic"
APPS_DIR="$DATA_HOME/applications"
DESKTOP_ENTRY_PATH="$APPS_DIR/k-sounds-hub.desktop"
DESKTOP_COMPAT_PATH="$APPS_DIR/ksounds-hub.desktop"
DESKTOP_GLASS_COMPAT_PATH="$APPS_DIR/ksounds-hub-glass.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -d "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"
DESKTOP_SHORTCUT_PATH="$DESKTOP_DIR/k-sounds-hub.desktop"

ASSUME_YES=0
INSTALL_SYSTEM_DEPS=0
INSTALL_DESKTOP=1
MAKE_DESKTOP_SHORTCUT=0
BUILD_NATIVE=1
BUILD_HUD=1
BUILD_GLOBAL_SHORTCUTS=1
REQUIRE_HUD=0
RUN_CHECK=1
INSTALL_COMMITTED=0
INSTALL_FINISHED=0

usage() {
  cat <<'USAGE'
K-Sounds Hub non-invasive user installer

Usage:
  ./install.sh [options]

Default behavior:
  - installs only into the current user's home directory
  - copies the app to ~/.local/share/k-sounds-hub/app
  - creates ~/.local/share/k-sounds-hub/app/.venv
  - creates ~/.local/bin/k-sounds-hub
  - creates ~/.local/bin/k-sounds-hub-classic as an explicit temporary fallback
  - creates ~/.local/share/applications/k-sounds-hub.desktop
  - leaves autostart disabled on a fresh install
  - preserves an existing enabled K-Sounds autostart during upgrades
  - does not write /etc, /usr, /opt, or global PipeWire config

Options:
      --install-system-deps  Install distro packages with sudo when dependencies are missing.
  -y, --yes                  Use non-interactive package manager mode where supported.
      --no-desktop           Do not install the app-menu launcher.
      --desktop-shortcut     Also create a desktop shortcut.
      --build-native         Build native engines (default; kept for compatibility).
      --no-native            Skip optional native engines; core PCM safety guard is still built.
      --build-hud            Try to build the Wayland HUD (default).
      --no-hud               Skip the optional Wayland HUD build.
      --require-hud          Fail installation instead of warning if the HUD cannot build.
      --no-check             Skip the final environment check.
  -h, --help                 Show this help.

Examples:
  ./install.sh
  ./install.sh --install-system-deps
  ./install.sh --install-system-deps --yes
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --install-system-deps) INSTALL_SYSTEM_DEPS=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --no-desktop) INSTALL_DESKTOP=0 ;;
    --desktop-shortcut) MAKE_DESKTOP_SHORTCUT=1 ;;
    --build-native) BUILD_NATIVE=1 ;;
    --no-native) BUILD_NATIVE=0 ;;
    --build-hud) BUILD_HUD=1 ;;
    --no-hud) BUILD_HUD=0; REQUIRE_HUD=0 ;;
    --require-hud) BUILD_HUD=1; REQUIRE_HUD=1 ;;
    --no-check) RUN_CHECK=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

log() { printf '\n===== %s =====\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }

select_app_dir() {
  APP_DIR="$1"
  VENV_DIR="$APP_DIR/.venv"
}

cleanup_install_state() {
  local rc=$?
  trap - EXIT

  if (( INSTALL_FINISHED == 0 )); then
    if (( INSTALL_COMMITTED == 1 )); then
      rm -rf -- "$FINAL_APP_DIR"
      if [[ -d "$PREVIOUS_APP_DIR" ]]; then
        mv -- "$PREVIOUS_APP_DIR" "$FINAL_APP_DIR"
      fi
    fi
    rm -rf -- "$STAGING_APP_DIR"
  else
    rm -rf -- "$STAGING_APP_DIR" "$PREVIOUS_APP_DIR"
  fi

  exit "$rc"
}

trap cleanup_install_state EXIT

run_root() {
  if (( EUID == 0 )); then
    "$@"
  elif need_cmd sudo; then
    sudo "$@"
  else
    echo "sudo is required for this optional system dependency step: $*" >&2
    exit 1
  fi
}

read_os_release() {
  OS_ID="unknown"
  OS_LIKE=""
  OS_NAME="unknown Linux"
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
    OS_NAME="${PRETTY_NAME:-${NAME:-unknown Linux}}"
  fi
}

pkg_args_yes() {
  local pm="$1"
  case "$pm" in
    pacman) (( ASSUME_YES )) && printf '%s\n' --noconfirm ;;
    apt) (( ASSUME_YES )) && printf '%s\n' -y ;;
    dnf) (( ASSUME_YES )) && printf '%s\n' -y ;;
    zypper) (( ASSUME_YES )) && printf '%s\n' --non-interactive ;;
  esac
}

install_system_deps() {
  read_os_release
  log "Installing system dependencies"
  echo "Detected: $OS_NAME"
  local id_like=" $OS_ID $OS_LIKE "

  if [[ "$id_like" == *" arch "* ]] || [[ "$id_like" == *" manjaro "* ]] || [[ "$OS_ID" == "endeavouros" ]]; then
    local yes_args=()
    mapfile -t yes_args < <(pkg_args_yes pacman)
    local packages=(
      python python-pip python-virtualenv \
      pipewire pipewire-pulse wireplumber libpulse \
      ffmpeg xdg-utils desktop-file-utils xcb-util-cursor util-linux kglobalaccel
    )
    packages+=(gcc)
    if (( BUILD_NATIVE || BUILD_HUD || BUILD_GLOBAL_SHORTCUTS )); then
      packages+=(cmake)
    fi
    if (( BUILD_HUD )); then
      packages+=(qt6-declarative layer-shell-qt)
    fi
    run_root pacman -S --needed "${yes_args[@]}" "${packages[@]}"
    return 0
  fi

  if [[ "$id_like" == *" debian "* ]] || [[ "$id_like" == *" ubuntu "* ]] || need_cmd apt-get; then
    local yes_args=()
    mapfile -t yes_args < <(pkg_args_yes apt)
    run_root apt-get update
    local packages=(
      python3 python3-venv python3-pip \
      pipewire pipewire-pulse wireplumber pipewire-bin pulseaudio-utils \
      ffmpeg libxcb-cursor0 xdg-utils desktop-file-utils util-linux libkf6globalaccel-dev
    )
    packages+=(build-essential)
    if (( BUILD_NATIVE || BUILD_HUD || BUILD_GLOBAL_SHORTCUTS )); then
      packages+=(cmake)
    fi
    if (( BUILD_HUD )); then
      packages+=(qt6-base-dev qt6-declarative-dev liblayershellqtinterface-dev)
    fi
    run_root apt-get install "${yes_args[@]}" "${packages[@]}"
    return 0
  fi

  if [[ "$id_like" == *" fedora "* ]] || [[ "$id_like" == *" rhel "* ]] || [[ "$id_like" == *" centos "* ]] || need_cmd dnf; then
    local yes_args=()
    mapfile -t yes_args < <(pkg_args_yes dnf)
    local packages=(
      python3 python3-pip python3-virtualenv \
      pipewire pipewire-pulseaudio wireplumber pulseaudio-utils \
      ffmpeg-free xdg-utils desktop-file-utils xcb-util-cursor util-linux kf6-kglobalaccel-devel
    )
    packages+=(gcc)
    if (( BUILD_NATIVE || BUILD_HUD || BUILD_GLOBAL_SHORTCUTS )); then
      packages+=(cmake gcc-c++)
    fi
    if (( BUILD_HUD )); then
      packages+=(qt6-qtdeclarative-devel layer-shell-qt-devel)
    fi
    run_root dnf install "${yes_args[@]}" "${packages[@]}"
    return 0
  fi

  if [[ "$id_like" == *" suse "* ]] || need_cmd zypper; then
    local yes_args=()
    mapfile -t yes_args < <(pkg_args_yes zypper)
    local packages=(
      python3 python3-pip python3-virtualenv \
      pipewire pipewire-pulseaudio wireplumber pulseaudio-utils \
      ffmpeg xdg-utils desktop-file-utils libxcb-cursor0 util-linux
    )
    packages+=(gcc)
    if (( BUILD_NATIVE || BUILD_HUD || BUILD_GLOBAL_SHORTCUTS )); then
      packages+=(cmake gcc-c++)
    fi
    run_root zypper "${yes_args[@]}" install --no-recommends "${packages[@]}"
    return 0
  fi

  cat >&2 <<'MSG'
Unsupported or unknown distribution for automatic dependency installation.
Install these manually, then rerun ./install.sh:
  python3.11+, python venv, pip, PipeWire, WirePlumber, PipeWire Pulse compatibility,
  pactl/parec client tools, pw-cat, ffmpeg, xdg-utils, desktop-file-utils.
MSG
  exit 1
}

find_python() {
  local candidates=(python3.13 python3.12 python3.11 python3 python)
  local c
  for c in "${candidates[@]}"; do
    need_cmd "$c" || continue
    if "$c" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

missing_runtime_commands() {
  local missing=()
  local cmd
  for cmd in pactl parec pw-cat ffmpeg; do
    need_cmd "$cmd" || missing+=("$cmd")
  done
  if ! need_cmd cc && ! need_cmd gcc; then
    missing+=("C compiler (cc/gcc)")
  fi
  if (( BUILD_NATIVE || BUILD_HUD || BUILD_GLOBAL_SHORTCUTS )); then
    need_cmd cmake || missing+=("cmake")
    if ! need_cmd c++ && ! need_cmd g++; then
      missing+=("C++ compiler")
    fi
  fi
  if (( BUILD_HUD )); then
    need_cmd flock || missing+=("flock")
  fi
  if ((${#missing[@]})); then
    printf '%s\n' "${missing[@]}"
  fi
}

print_dependency_help() {
  read_os_release
  cat <<EOF_HELP
Missing required runtime commands:
$(missing_runtime_commands | sed 's/^/  - /')

K-Sound Hub was not installed yet.

Recommended non-invasive flow:
  1. Install distro packages yourself, or rerun with:
       ./install.sh --install-system-deps

Detected distro:
  $OS_NAME
EOF_HELP
}

copy_app() {
  log "Preparing app in a transactional staging directory"
  mkdir -p "$APP_ROOT"
  rm -rf "$APP_DIR"
  mkdir -p "$APP_DIR"

  if need_cmd rsync; then
    rsync -a --delete \
      --exclude='.git/' \
      --exclude='.venv/' \
      --exclude='venv/' \
      --exclude='__pycache__/' \
      --exclude='.pytest_cache/' \
      --exclude='.ruff_cache/' \
      --exclude='.mypy_cache/' \
      --exclude='build/' \
      --exclude='dist/' \
      --exclude='*.pyc' \
      "$SOURCE_DIR/" "$APP_DIR/"
  else
    (cd "$SOURCE_DIR" && tar \
      --exclude='./.git' \
      --exclude='./.venv' \
      --exclude='./venv' \
      --exclude='./__pycache__' \
      --exclude='./.pytest_cache' \
      --exclude='./.ruff_cache' \
      --exclude='./.mypy_cache' \
      --exclude='./build' \
      --exclude='*/build' \
      --exclude='./dist' \
      -cf - .) | (cd "$APP_DIR" && tar -xf -)
  fi
}

activate_staged_app() {
  log "Activating the validated app"

  if [[ "${KSH_INSTALL_TEST_MODE:-0}" != "1" ]] \
    && [[ -x "$FINAL_APP_DIR/scripts/stop_all_ksound_runtime.sh" ]]; then
    "$FINAL_APP_DIR/scripts/stop_all_ksound_runtime.sh" >/dev/null 2>&1 || true
  fi

  rm -rf -- "$PREVIOUS_APP_DIR"
  if [[ -d "$FINAL_APP_DIR" ]]; then
    mv -- "$FINAL_APP_DIR" "$PREVIOUS_APP_DIR"
  fi

  mv -- "$STAGING_APP_DIR" "$FINAL_APP_DIR"
  select_app_dir "$FINAL_APP_DIR"
  INSTALL_COMMITTED=1
}

finish_install() {
  INSTALL_FINISHED=1
  rm -rf -- "$PREVIOUS_APP_DIR"
}

setup_python_env() {
  log "Creating Python virtual environment"
  local py
  py="$(find_python)" || {
    echo "Python 3.11+ was not found." >&2
    exit 1
  }
  echo "Using Python: $($py -c 'import sys; print(sys.executable, sys.version.split()[0])')"

  "$py" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
  "$VENV_DIR/bin/python" -m pip install -e "$APP_DIR"
}

install_wrapper() {
  log "Migrating user commands to the canonical Glass launcher"
  "$APP_DIR/scripts/migrate_user_integration.sh" "$APP_DIR" --no-desktop

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in PATH. Add it to PATH or launch with: $WRAPPER_PATH" ;;
  esac
}

install_desktop_launcher() {
  (( INSTALL_DESKTOP )) || return 0
  log "Installing app-menu launcher"
  mkdir -p "$APPS_DIR"
  local icon_path="$APP_DIR/src/ksound_hub/assets/app_icon.png"
  cat > "$DESKTOP_ENTRY_PATH" <<EOF_DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
Comment=PipeWire-first modular Linux audio hub
Exec=$WRAPPER_PATH
Icon=$icon_path
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
StartupWMClass=ksounds-hub
X-GNOME-UsesNotifications=true
EOF_DESKTOP
  chmod +x "$DESKTOP_ENTRY_PATH"

  local compatibility_path
  for compatibility_path in "$DESKTOP_COMPAT_PATH" "$DESKTOP_GLASS_COMPAT_PATH"; do
    cat > "$compatibility_path" <<EOF_COMPAT
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
Comment=Compatibility launcher for existing KDE favorites
Exec=$WRAPPER_PATH
Icon=$icon_path
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
StartupWMClass=ksounds-hub
NoDisplay=true
X-GNOME-UsesNotifications=true
EOF_COMPAT
    chmod +x "$compatibility_path"
  done

  if (( MAKE_DESKTOP_SHORTCUT )); then
    mkdir -p "$DESKTOP_DIR"
    cp "$DESKTOP_ENTRY_PATH" "$DESKTOP_SHORTCUT_PATH"
    chmod +x "$DESKTOP_SHORTCUT_PATH"
  fi

  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true
}

build_audio_safety_guard() {
  log "Building core finite-PCM safety guard"
  "$APP_DIR/scripts/build_ksound_audio_safety_guard.sh" "$APP_DIR"
}

build_native_engine() {
  (( BUILD_NATIVE )) || return 0
  log "Building native Micro, meter and playback engines"
  "$APP_DIR/scripts/build_ksound_native_engine.sh" "$APP_DIR"
}

build_global_shortcut_helper() {
  log "Building KDE-native global shortcut helper"
  if "$APP_DIR/scripts/build_kglobalaccel_helper.sh" "$APP_DIR"; then
    return 0
  fi

  warn "KGlobalAccel helper could not be built. KDE-native inline shortcut remapping will fall back to the XDG portal backend."
  warn "On Arch/EndeavourOS, rerun with --install-system-deps or install: kglobalaccel qt6-base cmake gcc"
}

build_hud_overlay() {
  (( BUILD_HUD )) || return 0
  log "Building optional Wayland HUD"

  if "$APP_DIR/scripts/build_hud_overlay.sh" "$APP_DIR"; then
    return 0
  fi

  if (( REQUIRE_HUD )); then
    echo "The HUD build failed and --require-hud was requested." >&2
    exit 1
  fi

  warn "The optional HUD could not be built. Glass and audio remain installed; the Overlay setting will need the Qt 6/LayerShellQt development packages."
}

run_final_check() {
  (( RUN_CHECK )) || return 0
  log "Final environment check"
  "$APP_DIR/scripts/check_k_sound_hub_env.sh" || warn "Environment check reported issues. The install files were still created."
}

main() {
  if (( EUID == 0 )) && [[ "${KSH_INSTALL_TEST_MODE:-0}" != "1" ]]; then
    echo "Do not run this installer as root. It is a user install." >&2
    echo "Use ./install.sh --install-system-deps if you want the script to call sudo only for packages." >&2
    exit 1
  fi

  if [[ ! -f "$SOURCE_DIR/pyproject.toml" || ! -d "$SOURCE_DIR/src/ksound_hub" ]]; then
    echo "This installer must be run from the extracted K-Sound Hub source/release directory." >&2
    exit 1
  fi

  if (( INSTALL_SYSTEM_DEPS )); then
    install_system_deps
  else
    local missing_file="/tmp/k-sounds-hub-missing.$$"
    missing_runtime_commands >"$missing_file"
    if [[ -s "$missing_file" ]]; then
      rm -f "$missing_file"
      print_dependency_help
      exit 2
    fi
    rm -f "$missing_file"
  fi

  select_app_dir "$STAGING_APP_DIR"
  copy_app
  setup_python_env
  build_audio_safety_guard
  build_native_engine
  build_global_shortcut_helper
  build_hud_overlay
  run_final_check
  activate_staged_app
  install_wrapper
  install_desktop_launcher
  finish_install

  log "Install complete"
  cat <<EOF_DONE
Installed app:
  $APP_DIR

User command:
  $WRAPPER_PATH

Explicit temporary classic fallback:
  $CLASSIC_WRAPPER_PATH

App menu launcher:
  $DESKTOP_ENTRY_PATH

User config will live in:
  $CONFIG_HOME/k-sounds-hub

Start with:
  k-sounds-hub

Fresh installs leave autostart disabled. An existing enabled K-Sounds autostart
is preserved during upgrades and can be changed from Glass Settings.
No global PipeWire config was written.

Native engines built: $([[ "$BUILD_NATIVE" == 1 ]] && echo yes || echo no)
KDE global-shortcut helper built: $([[ -x "$APP_DIR/bin/ksound-kglobalaccel-helper" ]] && echo yes || echo no)
Wayland HUD built: $([[ -x "$APP_DIR/tools/hud_overlay/build/audio-hud-overlay" ]] && echo yes || echo no)
EOF_DONE
}

main "$@"
