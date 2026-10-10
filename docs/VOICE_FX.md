# K-Sounds native Voice FX

IP13a adds the first native low-latency Voice FX chain to the existing MICRO engine.

## Design

Voice FX are independent from **TUNE / MICRO Processing**. The physical microphone is processed in this order:

1. optional TUNE input gain / RNNoise,
2. Hard Gate,
3. optional TUNE EQ / Compressor / De-esser,
4. **Voice FX**,
5. MICRO volume and final mix,
6. optional TUNE limiter on the complete virtual microphone mix.

Soundboard and injected playback channels are mixed **after** Voice FX, so memes/music are not accidentally transformed when a voice preset is active.

## Presets in IP13a

- **Radio Pilot** — communications band-pass with light saturation.
- **Telephone** — narrower intercom/telephone band with stronger saturation.
- **Church** — native feedback-delay reverb with a long tail.
- **Robot** — ring-modulated synthetic voice.

`Strength / wet mix` controls how much transformed signal is blended with the dry microphone. Preset and bypass changes are hot-reloaded from `micro_state.txt`; they do not rebuild PipeWire routes.

## Startup Soundboard hardening

Glass now performs two bounded, idempotent Soundboard route reconciliation passes after normal startup (280 ms and 950 ms). This is specifically to cover the observed first-launch race where MIC OUT worked but Soundboard -> MICRO did not until K-Sounds was restarted. Pad activation itself remains on the fast playback path and does not run route rebuilds.
