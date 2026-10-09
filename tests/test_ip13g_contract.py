from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
MODELS = (ROOT / "src/ksound_hub/models.py").read_text(encoding="utf-8")
PIPEWIRE = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")
NATIVE = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")
CORE_NATIVE = (ROOT / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")


def test_ip13g_build_family_marker():
    assert 'APP_BUILD = "IP' in GLASS


def test_church_pa_is_persisted_and_sent_to_native_engine():
    assert '("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")' in MODELS
    assert '"church_pa"' in PIPEWIRE
    assert '("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")' in GLASS


def test_church_pa_is_a_separate_ui_effect_and_exclusive_with_church():
    assert '("church_pa", "Church PA", "▣"' in VFX
    assert 'wanted in {"reverb", "church", "church_pa"}' in VFX
    assert 'key not in {"reverb", "church", "church_pa"}' in VFX
    assert '"church_pa": "Church PA"' in GLASS


def test_church_reverb_send_has_plosive_protection_without_altering_robot_1():
    assert 'voice_fx_highpass(input, 155.0f' in NATIVE
    assert 'De-plosive the reverb send only.' in NATIVE
    assert '(direct * 0.18f) + (predelayed * 0.08f) + (tail * 0.74f)' in NATIVE
    assert 'constexpr float carrier_hz = 68.0f;' in NATIVE
    assert 'wet[index] = std::tanh(sample * 1.8f) * carrier * 0.90f;' in NATIVE


def test_church_pa_models_microphone_loudspeaker_before_room():
    assert 'effect == "church_pa"' in NATIVE
    assert 'voice_fx_highpass(input, 165.0f' in NATIVE
    assert 'voice_fx_lowpass(speaker, 6500.0f' in NATIVE
    assert 'std::tanh(speaker * 2.7f) / std::tanh(2.7f)' in NATIVE
    assert 'direct = clampf(speaker, -1.0f, 1.0f);' in NATIVE
    assert 'const float pa_mix = direct + (predelayed * 0.08f) + (tail * 0.54f);' in NATIVE


def test_native_chain_and_probe_keep_five_effect_regression_and_nine_effect_runtime_catalog():
    assert 'VoiceFxDsp church_pa;' in NATIVE
    assert 'voice_fx_active(state, "church_pa")' in NATIVE
    assert 'const std::array<std::string, 5> presets' in CORE_NATIVE
    assert 'const std::array<std::string, 9> voice_fx_order' in CORE_NATIVE


def test_native_probe_covers_plosive_guard_for_both_church_modes():
    assert 'std::string("church"), std::string("church_pa")' in CORE_NATIVE
    assert '"_plosive_tail="' in CORE_NATIVE
    assert 'tail_peak > 0.80f' in CORE_NATIVE
    assert 'Keep incompatible pairs deterministic even for hand-edited state files.' in CORE_NATIVE
