from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
NATIVE = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")


def test_ip13h2_build_family_marker():
    assert 'APP_BUILD = "IP' in GLASS


def test_church_pa_normal_foreground_contract():
    assert 'direct = clampf(speaker, -1.0f, 1.0f);' in NATIVE
    assert 'const float pa_mix = direct + (predelayed * 0.08f) + (tail * 0.54f);' in NATIVE
    assert 'std::tanh(pa_mix * 1.08f) / std::tanh(1.08f)' in NATIVE
    assert '(direct * 0.38f)' not in NATIVE
