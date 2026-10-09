from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_rnnoise_and_micro_eq_native_contract():
    engine=(ROOT/'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    writer=(ROOT/'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    assert 'rnnoise_process_frame' in tune
    assert '--probe-rnnoise' in engine
    assert 'micro_tune.process_pre_gate(mic_chunk, tune);' in engine
    assert 'impl_->rnnoise.process(frames, settings.noise_suppression)' in tune
    assert 'apply_parametric_eq(frames, impl_->eq, settings)' in tune
    assert 'micro_tune.process_post_gate(mic_chunk, tune);' in engine
    assert 'parts[0] == "eq_bands"' in engine
    assert 'f"eq_bands\\t' in writer
    assert 'RNNoise backend: READY' in ui
    assert '"4 · MICRO EQ", "READY"' in ui

def test_rnnoise_bypass_keeps_strength_separate():
    engine=(ROOT/'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    writer=(ROOT/'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')
    assert 'noise_suppression_enabled' in engine
    assert 'noise_suppression_enabled\\t' in writer
    assert 'if (settings.noise_suppression_enabled)' in tune

def test_rnnoise_partial_mix_is_latency_aligned():
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    assert "dry_delay_" in tune
    assert "dry_delay_slot_" in tune
    assert "Two slots = 960 samples" in tune
    assert "dry + ((denoised - dry) * wet)" in tune

def test_deesser_uses_selective_sibilance_band_detector():
    engine=(ROOT/'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    assert "detector_top_lowpass" in tune
    assert "sibilance_energy" in tune
    assert "speech_energy" in tune
    assert "spectral_ratio" in tune
    assert "ratio_threshold" in tune
    assert "deesser_vowel_db" in engine
    assert "deesser_sibilant_db" in engine
    assert "deesser_selective" in engine
    assert "dsp.high_gain" in tune

def test_native_micro_exports_processing_gain_reduction_telemetry():
    engine=(ROOT/'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    assert 'float reduction_db{0.0f};' in tune
    assert 'compressor_reduction_db' in engine
    assert 'deesser_reduction_db' in engine
    assert 'limiter_reduction_db' in engine
    backend=(ROOT/'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')
    assert 'def native_micro_processing_activity' in backend
    assert 'payload.get("processing", {})' in backend

def test_processing_telemetry_has_peak_hold_and_limiter_ceiling():
    engine=(ROOT/'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
    tune=(ROOT/'native_engine/src/micro_tune_dsp.cpp').read_text(encoding='utf-8')
    writer=(ROOT/'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')
    assert 'meter_reduction_db' in tune
    assert 'meter_hold_chunks' in tune
    assert 'hold_for_chunks = 32' in tune
    assert '--probe-processing-telemetry' in engine
    assert 'limiter_ceiling_db' in tune
    assert 'limiter_ceiling_db\\t' in writer

def test_native_micro_processing_activity_reads_real_json_values(tmp_path):
    from ksound_hub.audio.pipewire_v2_final import PipeWireAudioEngine

    levels = tmp_path / "micro_levels.json"
    levels.write_text(
        '{"processing":{"compressor_reduction_db":6.28878,"deesser_reduction_db":1.25,"limiter_reduction_db":2.5}}',
        encoding="utf-8",
    )

    engine = object.__new__(PipeWireAudioEngine)
    engine._native_micro_levels_path = levels
    engine._native_micro_levels_cache_mtime_ns = 0
    engine._native_micro_levels_cache_payload = {}
    engine._native_micro_enabled = lambda: True

    activity = engine.native_micro_processing_activity()
    assert activity["compressor_reduction_db"] == 6.28878
    assert activity["deesser_reduction_db"] == 1.25
    assert activity["limiter_reduction_db"] == 2.5
