from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
NATIVE = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")
B3 = (ROOT / "tests/test_ip13b3_contract.py").read_text(encoding="utf-8")
C = (ROOT / "tests/test_ip13c_contract.py").read_text(encoding="utf-8")


def test_ip13g2_build_marker_remains_exposed():
    assert 'APP_BUILD = "' in GLASS
    assert 'APP_BUILD' in GLASS


def test_stale_church_contracts_are_migrated_to_behavioral_checks():
    assert "large church rather than a repeating robot echo" not in B3
    assert "large church rather than a repeating robot echo" not in C
    assert "voice_fx_lowpass(tail, 5200.0f" not in C
    assert "De-plosive the reverb send only." in B3
    assert "church_pa" in B3
    assert "pa_mode ? 4400.0f : 5100.0f" in C


def test_church_pa_and_robot_guard_remain_in_native_dsp():
    assert 'effect == "church" || effect == "church_pa"' in NATIVE
    assert 'De-plosive the reverb send only.' in NATIVE
    assert 'constexpr float carrier_hz = 68.0f;' in NATIVE
    assert 'wet[index] = std::tanh(sample * 1.8f) * carrier * 0.90f;' in NATIVE
