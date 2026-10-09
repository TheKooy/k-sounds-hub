# K-Sounds Hub v0.4.7 — Glass / Native Audio checkpoint

Linux desktop release based on a tested IP15m12 release checkpoint.

## Changes

- Glass desktop UI and optimized 48-segment stereo peak meters at 40 Hz.
- Calibrated sample-peak meter display (dBFS), with no arbitrary per-channel offset.
- Native microphone fail-closed device selection and audio stability improvements.
- Modular host foundations and optional desktop/Remote extension contracts.
- Soundboard Remote integration and Android source improvements.

## Validation and scope

- Tested chiefly on EndeavourOS / Arch Linux, KDE Plasma / Wayland, PipeWire.
- Playback meters checked on five buses with 25 reference-signal checks.
- 27 targeted integration tests passed in the development checkpoint.
- Native MICRO and MIC OUT path-specific absolute calibration remains future work.
- Android Remote is versioned separately (currently 0.4.9-edge-dev2). No Android APK is bundled in this Linux release unless explicitly provided as a separate signed release asset.
- Optional external modules remain preview interfaces; not part of the default installation.

Only release archives attached to GitHub Releases are recommended for end-user installation.
