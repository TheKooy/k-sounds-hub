#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d /tmp/k-sounds-install-lifecycle.XXXXXX)"
TEST_HOME="$TEST_ROOT/home"
STUB_DIR="$TEST_ROOT/bin"
DATA_HOME="$TEST_HOME/.local/share"
CONFIG_HOME="$TEST_HOME/.config"
APP_ROOT="$DATA_HOME/k-sounds-hub"
APP_DIR="$APP_ROOT/app"
BIN_DIR="$TEST_HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
FAKE_PYTHON="$REPO_DIR/tests/fixtures/fake_installer_python.sh"
PYTHON_LOG="$TEST_ROOT/python-modules.log"

cleanup() {
  if [[ "$TEST_ROOT" == /tmp/k-sounds-install-lifecycle.* ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}
trap cleanup EXIT

fail() {
  echo "install lifecycle test failed: $*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || fail "missing file: $1"
}

require_executable() {
  [[ -x "$1" ]] || fail "missing executable: $1"
}

mkdir -p "$TEST_HOME/Desktop" "$STUB_DIR" "$CONFIG_HOME" "$DATA_HOME"
chmod +x "$FAKE_PYTHON"

for name in python3.13 python3.12 python3.11 python3 python; do
  ln -s "$FAKE_PYTHON" "$STUB_DIR/$name"
done

TRUE_BIN="$(type -P true)"
FALSE_BIN="$(type -P false)"
[[ -x "$TRUE_BIN" && -x "$FALSE_BIN" ]] || fail "true/false system binaries not found"

for name in pactl parec pw-cat ffmpeg systemctl update-desktop-database kbuildsycoca6; do
  ln -s "$TRUE_BIN" "$STUB_DIR/$name"
done
ln -s "$FALSE_BIN" "$STUB_DIR/pgrep"

test_env=(
  "HOME=$TEST_HOME"
  "XDG_DATA_HOME=$DATA_HOME"
  "XDG_CONFIG_HOME=$CONFIG_HOME"
  "PATH=$STUB_DIR:$PATH"
  "KSH_INSTALL_TEST_MODE=1"
  "KSH_FAKE_PYTHON_LOG=$PYTHON_LOG"
)

run_install() {
  env "${test_env[@]}" "$REPO_DIR/install.sh" --no-native --no-hud --no-check
}

run_install

require_file "$APP_DIR/pyproject.toml"
require_executable "$APP_DIR/.venv/bin/python"
require_executable "$BIN_DIR/k-sounds-hub"
require_executable "$BIN_DIR/k-sounds-hub-classic"
require_file "$APPS_DIR/k-sounds-hub.desktop"
require_file "$APPS_DIR/ksounds-hub.desktop"
require_file "$APPS_DIR/ksounds-hub-glass.desktop"

grep -Fq "$APP_DIR/scripts/start_ksound_hub_v2.sh" "$BIN_DIR/k-sounds-hub" \
  || fail "canonical wrapper does not target the installed app"
grep -Fq "KSH_FRONTEND_MODULE=ksound_hub.app" "$BIN_DIR/k-sounds-hub-classic" \
  || fail "classic fallback is not explicit"
grep -Fq "Exec=$BIN_DIR/k-sounds-hub" "$APPS_DIR/k-sounds-hub.desktop" \
  || fail "desktop launcher does not use the canonical wrapper"
grep -Fq "Icon=$APP_DIR/src/ksound_hub/assets/app_icon.png" "$APPS_DIR/k-sounds-hub.desktop" \
  || fail "desktop launcher icon does not target the installed app"
grep -Fq "NoDisplay=true" "$APPS_DIR/ksounds-hub.desktop" \
  || fail "Qt/KDE desktop-id compatibility launcher is visible"
grep -Fq "Exec=$BIN_DIR/k-sounds-hub" "$APPS_DIR/ksounds-hub-glass.desktop" \
  || fail "legacy KDE favorite does not use the canonical wrapper"

if [[ -d "$AUTOSTART_DIR" ]] && find "$AUTOSTART_DIR" -type f -print -quit | grep -q .; then
  fail "fresh install created an autostart entry"
fi

env "${test_env[@]}" KSH_START_SOUNDBOARD_REMOTE=0 "$BIN_DIR/k-sounds-hub"
grep -Fxq "ksound_hub.glass_app" "$PYTHON_LOG" \
  || fail "canonical wrapper did not launch Glass"

mkdir -p "$CONFIG_HOME/k-sounds-hub" "$APP_ROOT/retired/test"
printf '%s\n' keep > "$CONFIG_HOME/k-sounds-hub/preserved-marker"
printf '%s\n' keep > "$APP_ROOT/retired/test/preserved-marker"
printf '%s\n' old > "$APP_DIR/rollback-marker"

if env "${test_env[@]}" KSH_FAKE_PYTHON_FAIL_INSTALL=1 \
  "$REPO_DIR/install.sh" --no-native --no-hud --no-check; then
  fail "forced dependency failure unexpectedly succeeded"
fi

require_file "$APP_DIR/rollback-marker"
require_file "$CONFIG_HOME/k-sounds-hub/preserved-marker"
require_file "$APP_ROOT/retired/test/preserved-marker"

mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/ksounds-hub.desktop" <<EOF_AUTOSTART
[Desktop Entry]
Type=Application
Name=K-Sounds Hub Glass
Exec=$BIN_DIR/ksounds-hub-glass
X-GNOME-Autostart-enabled=true
EOF_AUTOSTART

run_install

[[ ! -e "$APP_DIR/rollback-marker" ]] \
  || fail "successful upgrade kept stale app files"
require_file "$CONFIG_HOME/k-sounds-hub/preserved-marker"
require_file "$APP_ROOT/retired/test/preserved-marker"
require_file "$AUTOSTART_DIR/k-sounds-hub.desktop"
grep -Fq "Exec=$BIN_DIR/k-sounds-hub" "$AUTOSTART_DIR/k-sounds-hub.desktop" \
  || fail "upgrade did not migrate autostart to the canonical wrapper"
[[ ! -e "$AUTOSTART_DIR/ksounds-hub.desktop" ]] \
  || fail "upgrade kept the legacy autostart identifier active"

env "${test_env[@]}" "$APP_DIR/uninstall.sh"

[[ ! -e "$APP_DIR" ]] || fail "uninstall kept the installed app"
[[ ! -e "$BIN_DIR/k-sounds-hub" ]] || fail "uninstall kept the canonical wrapper"
[[ ! -e "$APPS_DIR/k-sounds-hub.desktop" ]] || fail "uninstall kept the desktop launcher"
[[ ! -e "$APPS_DIR/ksounds-hub.desktop" ]] || fail "uninstall kept the KDE compatibility launcher"
[[ ! -e "$AUTOSTART_DIR/k-sounds-hub.desktop" ]] || fail "uninstall kept the autostart entry"
require_file "$CONFIG_HOME/k-sounds-hub/preserved-marker"
require_file "$APP_ROOT/retired/test/preserved-marker"

env "${test_env[@]}" "$REPO_DIR/uninstall.sh" >/dev/null
require_file "$CONFIG_HOME/k-sounds-hub/preserved-marker"
require_file "$APP_ROOT/retired/test/preserved-marker"

echo "install lifecycle: fresh opt-in install, KDE aliases, autostart-preserving upgrade, rollback and safe uninstall passed"
