# Installation

K-Sounds Hub is installed from GitHub Releases.

## What to download

On the GitHub Releases page, download one of these assets:

- k-sounds-hub-linux-release-vX.Y.Z.tar.gz
- k-sounds-hub-linux-release-vX.Y.Z.zip

Do not use the green Code button for normal installation.
Do not download the automatic Source code zip or Source code tar.gz files.

## Install from tar.gz

Open a terminal in the download folder, then run:

    tar -xzf k-sounds-hub-linux-release-vX.Y.Z.tar.gz
    cd k-sounds-hub-linux-release-vX.Y.Z
    ./install.sh

Start the app with:

    k-sounds-hub

## Install from zip

Open a terminal in the download folder, then run:

    unzip k-sounds-hub-linux-release-vX.Y.Z.zip
    cd k-sounds-hub-linux-release-vX.Y.Z
    ./install.sh

Start the app with:

    k-sounds-hub

## Important notes

- Run install.sh from a terminal.
- Double-clicking install.sh in a file manager may look like nothing happened.
- install.sh installs the app but does not automatically launch it.
- If the command is not found after install, reopen the terminal or run:

    ~/.local/bin/k-sounds-hub

## If dependencies are missing

The installer checks for required commands such as pactl, parec, pw-cat and ffmpeg.

To let the installer install distro packages with sudo:

    ./install.sh --install-system-deps

For non-interactive package installation where supported:

    ./install.sh --install-system-deps --yes

## What gets installed

Default install paths:

    ~/.local/share/k-sounds-hub/app        application files
    ~/.local/share/k-sounds-hub/app/.venv  Python virtual environment
    ~/.local/bin/k-sounds-hub              user command
    ~/.local/bin/ksound-hub-v2             compatibility alias to Glass
    ~/.local/bin/k-sounds-hub-classic      explicit temporary classic fallback
    ~/.local/share/applications/k-sounds-hub.desktop
    ~/.config/k-sounds-hub                 user config, created at runtime

The installer does not:

- write to /etc
- write to /usr
- write to /opt
- enable autostart
- write global PipeWire configuration
- remove distro packages during uninstall
- start EasyEffects automatically

The application menu and the `k-sounds-hub` command launch Glass. The old frontend is not started implicitly.

The three native runtime components (Micro, meters and the disabled-by-default
playback engine) are built during installation. The validated native Micro
path is used automatically; if it cannot start, K-Sounds falls back to its
PipeWire loopback implementation. Use `--no-native` only for diagnostics or a
minimal fallback install.

The installer also tries to build the optional Wayland HUD. A missing HUD
development dependency produces a warning without cancelling the audio app.
Use `--no-hud` to skip that build or `--require-hud` to make it mandatory.

## Uninstall

    ~/.local/share/k-sounds-hub/app/uninstall.sh

Remove saved config too:

    ~/.local/share/k-sounds-hub/app/uninstall.sh --remove-config

Backups moved into `~/.local/share/k-sounds-hub/retired` are deliberately
preserved. Delete those too only with:

    ~/.local/share/k-sounds-hub/app/uninstall.sh --purge-retired

## Android remote

The Android remote is a separate asset:

    KSoundsSoundboardRemote.apk

Install it manually on Android.

The Remote server is owned by the K-Sounds runtime: it starts once with Glass,
is health-checked before reuse, and stops when K-Sounds quits. No separate
systemd service or login autostart is installed. The default idle timeout is
disabled while K-Sounds is running; advanced users can set
`KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS` explicitly.
