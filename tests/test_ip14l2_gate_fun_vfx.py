from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def manifest(module_id: str) -> dict:
    return json.loads(read(f"src/ksound_hub/builtin_modules/{module_id}/module.json"))


def test_release_versions_and_module_ownership() -> None:
    glass = read("src/ksound_hub/glass_app.py")
    remote = read("tools/soundboard_remote/ksound_soundboard_web.py")
    assert 'APP_BUILD = "IP' in glass
    assert 'Hub IP14' in remote
    assert manifest("micro_tune")["version"] == "1.1.0"
    assert manifest("voice_fx")["version"] == "1.2.0"
    assert manifest("hard_gate")["version"] == "1.0.0"
    assert "native_engine/src/micro_tune_dsp.cpp" in manifest("micro_tune")["owned_paths"]
    assert "native_engine/src/voice_fx_dsp.cpp" in manifest("voice_fx")["owned_paths"]
    assert "native_engine/src/hard_gate_dsp.cpp" in manifest("hard_gate")["owned_paths"]


def test_smooth_gate_state_roundtrip_and_native_writer_contract() -> None:
    models = read("src/ksound_hub/models.py")
    writer = read("src/ksound_hub/audio/pipewire_v2_final.py")
    native = read("native_engine/src/micro_engine.cpp")
    tune = read("native_engine/src/micro_tune_dsp.cpp")
    tune_header = read("native_engine/src/micro_tune_dsp.hpp")

    assert "micro_gate_enabled: bool = False" in models
    assert "micro_gate_threshold: int = 35" in models
    assert 'data.get("micro_gate_enabled", False)' in models
    assert 'data.get("micro_gate_threshold", 35)' in models
    assert '"micro_gate_enabled": bool(self.micro_gate_enabled)' in models
    assert '"micro_gate_threshold": max(0, min(100, int(self.micro_gate_threshold)))' in models

    assert 'f"gate_enabled\\t{1 if bool(getattr(channel, \'micro_gate_enabled\', False)) else 0}"' in writer
    assert 'f"gate_threshold\\t{max(0, min(100, int(getattr(channel, \'micro_gate_threshold\', 35))))}"' in writer
    assert 'parts[0] == "gate_enabled"' in native
    assert 'parts[0] == "gate_threshold"' in native
    assert "bool gate_enabled{false};" in tune_header
    assert "float gate_threshold{35.0f};" in tune_header

    assert "struct SoftGateState" in tune
    assert "dsp.hold_chunks = 12" in tune
    assert "db_to_gain(-26.0f)" in tune
    assert "0.120f" in tune
    assert "apply_soft_gate(frames, impl_->gate, settings);" in tune
    post = tune[tune.index("void Processor::process_post_gate"):]
    assert post.index("apply_soft_gate") < post.index("apply_parametric_eq") < post.index("apply_compressor") < post.index("apply_deesser")


def test_hard_gate_stays_independent_and_in_the_same_audio_position() -> None:
    engine = read("native_engine/src/micro_engine.cpp")
    hard_gate = read("native_engine/src/hard_gate_dsp.hpp")
    glass = read("src/ksound_hub/glass_app.py")

    loop = engine[engine.index("micro_tune.process_pre_gate"):engine.index("mix_add_ramped", engine.index("micro_tune.process_pre_gate"))]
    assert loop.index("micro_tune.process_pre_gate") < loop.index("micro_gate.process") < loop.index("micro_tune.process_post_gate") < loop.index("micro_voice_fx.process")
    assert "static float threshold_to_peak" in hard_gate
    assert 'module_enabled("hard_gate")' in glass
    assert 'module_enabled("micro_tune")' in glass
    assert 'if not self.module_enabled("hard_gate"):' in glass


def test_fun_native_voice_fx_and_exclusivity_contract() -> None:
    engine = read("native_engine/src/micro_engine.cpp")
    dsp = read("native_engine/src/voice_fx_dsp.cpp")
    models = read("src/ksound_hub/models.py")
    ui = read("src/ksound_hub/voice_fx_ui.py")
    remote = read("tools/soundboard_remote/ksound_soundboard_web.py")

    order = '("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")'
    assert order in models
    assert 'const std::array<std::string, 9> voice_fx_order' in engine
    assert '"distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa"' in engine
    assert '--probe-fun-voice-fx' in engine

    for marker in (
        'VoiceFxDsp distortion;',
        'VoiceFxDsp pitch_down;',
        'VoiceFxDsp pitch_up;',
        'VoiceFxDsp reverb;',
        'effect == "distortion"',
        'effect == "pitch_down"',
        'effect == "pitch_up"',
        'effect == "reverb"',
    ):
        assert marker in dsp

    for key, label in (
        ("distortion", "Distortion"),
        ("pitch_down", "Pitch ↓"),
        ("pitch_up", "Pitch ↑"),
        ("reverb", "Reverb"),
    ):
        assert f'("{key}", "{label}"' in ui
        assert f'("{key}", "{label}"' in remote

    assert "Pitch ↑/↓ replace one another" in ui
    assert "Reverb /\n    Church / Church PA are mutually exclusive" in ui
    assert "custom_presets_provider" in ui
    assert "Custom presets" in ui


def test_micro_card_layout_and_visual_identity() -> None:
    glass = read("src/ksound_hub/glass_app.py")

    assert 'QPushButton("Hard Gate", self)' in glass
    assert "button.move(5, top_y)" in glass
    assert "fx_button.move(right_x, top_y)" in glass
    assert "top_y + 23" in glass
    assert 'self.setMinimumWidth(168 if self.channel_key == "micro" else 78)' in glass

    # Hard Gate keeps the old purple card treatment; Voice FX now owns yellow.
    assert 'QFrame#channelCard[hardGate="true"]' in glass
    assert "background: rgba(56, 18, 128, 78);" in glass
    assert 'QFrame#channelCard[voiceFxActive="true"]' in glass
    assert "rgba(255, 211, 72, 82)" in glass
    assert "rgba(255, 207, 66, 100)" in glass
    assert "def micro_voice_fx_any_active(self) -> bool:" in glass
    assert 'bool(custom.get("enabled"))' in glass


def test_tune_ui_uses_soft_gate_and_keeps_hard_gate_outside() -> None:
    ui = read("src/ksound_hub/micro_processing_ui.py")
    assert '"3 · Gate", "READY · smooth"' in ui
    assert 'VisibleCheckBox("Enable Gate")' in ui
    assert '"gate_enabled"' in ui
    assert '"gate_threshold"' in ui
    assert "card-level Hard Gate is separate" in ui
    assert '"3 · Hard Gate"' not in ui


def test_native_probes_cover_new_features_without_replacing_legacy_probes() -> None:
    native = read("native_engine/src/micro_engine.cpp")
    assert '--probe-soft-gate' in native
    assert 'soft_gate=' in native
    assert '--probe-fun-voice-fx' in native
    assert 'fun_voice_fx=' in native
    # The old five-effect regression probe deliberately remains intact.
    assert 'const std::array<std::string, 5> presets' in native
