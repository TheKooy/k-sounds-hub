# K-Sounds Hub

K-Sounds Hub is a PipeWire-first modular Linux audio hub. Glass is its official desktop frontend.

It provides a focused desktop GUI for Linux audio routing, per-channel control, persistent settings, optional overlay feedback, IPC control shortcuts, live level meters, and PipeWire-based app routing helpers.

This project is being developed with AI assistance.

## Status

Current public version: 0.4.7

Latest Linux release: v0.4.7

Main tested environment:

- EndeavourOS / Arch Linux
- KDE Plasma / Wayland
- PipeWire
- WirePlumber
- Python 3.11+

Other major Linux distributions are supported on a best-effort basis through the non-invasive installer.

## Features

- fixed channel set: ALL, GAME, CHAT, MEDIA, MORE, MICRO, MIC OUT
- per-channel volume and mute
- per-channel EQ profile selection and editing
- live signal meter widgets
- optional on-screen overlay feedback
- IPC control for external shortcuts
- optional wallpaper/background styling
- tray-capable close behavior
- optional Android soundboard remote source
- native MICRO processing foundation: hard gate, input gain, compressor, de-esser and final limiter
- Android Remote updater with signed in-place updates through public GitHub release assets

## Recommended user installation

For normal users, install K-Sounds Hub from the GitHub Releases page.

Important:

- Do not use the green Code button for normal installation.
- Do not download the automatic Source code zip or Source code tar.gz files.
- Download one of the release assets instead:
  - k-sounds-hub-linux-release-vX.Y.Z.tar.gz
  - k-sounds-hub-linux-release-vX.Y.Z.zip

### Install from the tar.gz release

Open a terminal in the folder where you downloaded the file, then run:

    tar -xzf k-sounds-hub-linux-release-vX.Y.Z.tar.gz
    cd k-sounds-hub-linux-release-vX.Y.Z
    ./install.sh

Then start the app with:

    k-sounds-hub

### Install from the zip release

Open a terminal in the folder where you downloaded the file, then run:

    unzip k-sounds-hub-linux-release-vX.Y.Z.zip
    cd k-sounds-hub-linux-release-vX.Y.Z
    ./install.sh

Then start the app with:

    k-sounds-hub

### Notes

- Run install.sh from a terminal, not by double-clicking it in the file manager.
- install.sh installs the app but does not automatically launch it.
- If k-sounds-hub is not found after installation, close and reopen the terminal, or run:

    ~/.local/bin/k-sounds-hub

The default install is intentionally non-invasive.

It installs into:

    ~/.local/share/k-sounds-hub/app
    ~/.local/share/k-sounds-hub/app/.venv
    ~/.local/bin/k-sounds-hub
    ~/.local/share/applications/k-sounds-hub.desktop
    ~/.config/k-sounds-hub

It does not:

- write to /etc
- write to /usr
- write to /opt
- enable autostart
- write global PipeWire configuration
- remove distro packages during uninstall
- start EasyEffects automatically

`k-sounds-hub` always opens Glass. The temporary classic frontend is available only through the explicit `k-sounds-hub-classic` command.

The installer builds the native Micro and meter components by default and
keeps an automatic Python/PipeWire fallback if the Micro binary cannot start.
The experimental native playback engine remains disabled. The Wayland HUD is
built on a best-effort basis unless `--require-hud` is requested.

The Soundboard Remote server follows the Glass runtime and is not installed as
a separate login service. A single health-checked manager owns its startup and
shutdown.

If required system dependencies are missing, the installer stops and tells you what is missing.

To let the installer install distro packages with sudo:

    ./install.sh --install-system-deps

For non-interactive package installation where supported:

    ./install.sh --install-system-deps --yes

See docs/INSTALL.md for details.

## Uninstall

    ~/.local/share/k-sounds-hub/app/uninstall.sh

Remove saved config too:

    ~/.local/share/k-sounds-hub/app/uninstall.sh --remove-config

Retired migration backups are kept by default and are removed only with the
explicit `--purge-retired` option.

## Runtime dependencies

Required commands:

- python3 3.11 or newer
- pactl
- parec
- pw-cat
- ffmpeg

Expected audio stack:

- PipeWire
- PipeWire PulseAudio compatibility
- WirePlumber

Python dependencies are installed into the app-local virtual environment:

- PySide6
- NumPy

## Audio stability note

For heavy live use, such as K-Sounds Hub plus a game plus Discord/TeamSpeak plus virtual microphone routing, a PipeWire quantum of `512 / 48000` is recommended. This keeps latency reasonable while avoiding crackles seen on more aggressive `256 / 48000` setups.

## Development installation

For development, clone the repository and use an editable Python install:

    git clone https://github.com/TheKooy/k-sounds-hub.git
    cd k-sounds-hub
    python -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip setuptools wheel
    python -m pip install -e .[dev]

Run checks:

    python -m pytest
    ./scripts/check_k_sound_hub_env.sh

Start from the source tree:

    ./scripts/start_ksound_hub_v2.sh

## Packaging a release

Maintainers can create local release archives with:

    ./scripts/package_release.sh

This creates:

    dist/k-sounds-hub-linux-release-vX.Y.Z.tar.gz
    dist/k-sounds-hub-linux-release-vX.Y.Z.zip
    dist/SHA256SUMS.txt

Release archives and APK files should be uploaded as GitHub Release assets, not committed to Git.

See docs/GITHUB_RELEASE.md for details.

## Android soundboard remote

The Android soundboard source is included in the repository.

Generated APK files are optional release assets and should not be committed to Git.

## Repository

Repository: https://github.com/TheKooy/k-sounds-hub

Default public branch: main

Current active development branch: feature/native-micro-engine

## Disclaimer

K-Sounds Hub interacts with the user's Linux audio session through PipeWire/PulseAudio-compatible tools. It is intended to be non-destructive, but it is still audio-routing software. Review scripts before running them on important systems.

## Soundboard Remote current state

The Soundboard is shared by Glass, the PC detached window, the web Remote and the Android WebView through one non-UI service. Current development behavior includes bulk local imports, folders, URL/API banks, cached video thumbnails, automatic fallback colours, gradual Stop All, explicit Edit selection, atomic Remote restart/recovery and persistent pad/folder order.

Remote gestures are intentionally touch-first: ordinary swipes scroll, pinch resizes the live grid up to 13 pads per row, normal-mode long-hold inserts a pad at the cyan drop line, and Edit mode uses explicit selection with `Swap selected` only when exactly two pads are selected. The optional `Resize outside Edit` setting enables pinch outside Edit without stealing one-finger scroll.

Android is a thin native shell around the Remote UI. Most Remote visual changes are served by the PC and do not require a new APK. Native Android changes (pairing storage, display-rate handling, permissions, updater behavior) do require a rebuilt APK. The updater uses public GitHub Release assets and Android in-place installation so pairing is preserved.

The Remote server is owned by the Glass runtime and supervised by a health-checked manager rather than a separate login service.

## MICRO processing development

The native MICRO engine is now the canonical physical-microphone path. Its product-facing processing chain is being built as:

`Input Gain → Noise Suppression → Hard Gate → MICRO EQ → Compressor → De-esser → Voice FX → Limiter → MICRO`

The current IP11a foundation implements Input Gain, the existing Hard Gate, Compressor, De-esser and a smooth final Limiter. Noise Suppression is intentionally shown as unavailable until the optional RNNoise backend is actually linked; K-Sounds does not pretend a generic filter is AI noise suppression. MICRO EQ and Voice FX remain follow-up stages.

EasyEffects is still an optional external source during this migration. Do not remove it from an existing working setup until the native K-Sounds chain has been validated on that microphone. Disabled native processing blocks are real bypasses.

### MICRO processing safety / A-B workflow

The MICRO `FX` panel has a persistent master `Processing` bypass. It defaults OFF while the native chain is under validation. Settings can be prepared while bypassed, then enabled for immediate A/B comparison. The already-validated Hard Gate remains independent. EasyEffects may stay installed and available as a fallback source throughout migration/testing.
