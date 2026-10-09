from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MICRO = (ROOT / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")
TUNE = (ROOT / "native_engine/src/micro_tune_dsp.cpp").read_text(encoding="utf-8")
TUNE_HPP = (ROOT / "native_engine/src/micro_tune_dsp.hpp").read_text(encoding="utf-8")
CMAKE = (ROOT / "native_engine/CMakeLists.txt").read_text(encoding="utf-8")
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def test_ip14j_is_core_refactor_without_fake_tune_version_bump():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'Hub IP14' in WEB
    manifest = json.loads(
        (ROOT / "src/ksound_hub/builtin_modules/micro_tune/module.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "1.1.0"
    assert manifest["ksounds_api"] == 1


def test_micro_tune_native_dsp_is_a_separate_translation_unit():
    assert '#include "micro_tune_dsp.hpp"' in MICRO
    assert "src/micro_tune_dsp.cpp" in CMAKE
    assert "class Processor" in TUNE_HPP
    assert "struct Processor::Impl" in TUNE
    assert "RNNoiseBackend" in TUNE
    assert "ParametricEqState" in TUNE
    assert "CompressorState" in TUNE
    assert "DeEsserState" in TUNE
    assert "LimiterState" in TUNE


def test_core_no_longer_owns_tune_or_hard_gate_algorithms():
    for marker in (
        "class RNNoiseBackend",
        "struct ParametricEqState",
        "struct CompressorState",
        "struct DeEsserState",
        "struct LimiterState",
        "void apply_parametric_eq(",
        "void apply_compressor(",
        "void apply_deesser(",
        "void apply_limiter(",
    ):
        assert marker not in MICRO
    assert "struct HardGateState" not in MICRO
    assert "apply_hard_gate_to_micro_chunk" not in MICRO
    assert '#include "hard_gate_dsp.hpp"' in MICRO


def test_pipeline_order_preserves_independent_hard_gate_and_voice_fx():
    pre = MICRO.index("micro_tune.process_pre_gate(mic_chunk, tune);")
    gate = MICRO.index("micro_gate.process(mic_chunk, hard_gate_settings(state));", pre)
    post = MICRO.index("micro_tune.process_post_gate(mic_chunk, tune);", gate)
    voice = MICRO.index(
        "micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);",
        post,
    )
    custom = MICRO.index(
        "micro_custom_fx.process_custom(mic_chunk, state.custom_fx_enabled, state.custom_fx_wet, state.custom_fx_modules);",
        voice,
    )
    physical_mix = MICRO.index(
        "mix_add_ramped(micro_mix, mic_chunk, micro_gain_current, mic_gain);",
        custom,
    )
    sends = MICRO.index("for (const auto& send : state.sends)", physical_mix)
    limiter = MICRO.index("micro_tune.process_final_mix(micro_mix, micro_tune_settings(state));", sends)
    assert pre < gate < post < voice < custom < physical_mix < sends < limiter


def test_micro_tune_manifest_owns_native_files_and_core_transport_is_foreign():
    manifest = json.loads(
        (ROOT / "src/ksound_hub/builtin_modules/micro_tune/module.json").read_text(encoding="utf-8")
    )
    assert "native_engine/src/micro_tune_dsp.cpp" in manifest["owned_paths"]
    assert "native_engine/src/micro_tune_dsp.hpp" in manifest["owned_paths"]
    assert "native_engine/src/micro_engine.cpp" not in manifest["owned_paths"]
    assert "native_engine/src/micro_engine.cpp" not in manifest["shared_paths"]


def test_existing_native_probes_stay_core_owned():
    for marker in (
        "--probe-micro-tune",
        "--probe-rnnoise",
        "--probe-processing-telemetry",
        "--probe-voice-fx",
        "--probe-custom-fx",
    ):
        assert marker in MICRO
    assert "probe.rnnoise_available()" in MICRO
    assert "compressor_db=" in MICRO
    assert "deesser_vowel_db=" in MICRO
    assert "deesser_sibilant_db=" in MICRO
    assert "limiter_db=" in MICRO


def test_processing_telemetry_bridge_reads_module_owned_state():
    assert "ksound::micro_tune::Telemetry" in MICRO
    assert "micro_tune.telemetry()" in MICRO
    assert "compressor_reduction_db" in MICRO
    assert "deesser_reduction_db" in MICRO
    assert "limiter_reduction_db" in MICRO
