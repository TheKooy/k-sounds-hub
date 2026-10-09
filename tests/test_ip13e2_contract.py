from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")

def test_ip13e2_build_marker_survives_later_ip13e_versions():
    assert 'APP_BUILD = "IP' in GLASS

def test_human_case_tune_contract_is_migrated():
    assert 'QPushButton("Tune", self)' in GLASS

def test_voice_fx_independence_contract_accepts_human_case_tune():
    assert 'Tune remains independent.' in VFX
