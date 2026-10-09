from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")
NATIVE = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")


def test_ip13h_build_family_marker():
    assert 'APP_BUILD = "IP' in GLASS


def test_church_pa_keeps_foreground_pa_voice_at_normal_level():
    assert 'direct = clampf(speaker, -1.0f, 1.0f);' in NATIVE
    assert 'const float pa_mix = direct + (predelayed * 0.08f) + (tail * 0.54f);' in NATIVE
    assert 'std::tanh(pa_mix * 1.08f) / std::tanh(1.08f)' in NATIVE
    assert '(direct * 0.38f)' not in NATIVE


def test_church_pa_room_level_and_other_fx_guards_remain():
    assert 'predelayed * 0.08f' in NATIVE
    assert 'tail * 0.54f' in NATIVE
    assert 'constexpr float carrier_hz = 68.0f;' in NATIVE
    assert 'wet[index] = std::tanh(sample * 1.8f) * carrier * 0.90f;' in NATIVE
    assert 'Church PA' in VFX
    assert 'Normal-level PA/micro voice' in VFX
