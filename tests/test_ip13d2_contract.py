from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')


def test_ip13d2_build_marker_survives_later_ip13_versions():
    assert 'APP_BUILD = "IP' in GLASS


def test_embedded_tune_keeps_live_native_activity_callback():
    assert 'activity_callback=self._backend_controller.micro_processing_activity' in GLASS
    tune_block = GLASS.split('def _tune_page(self)', 1)[1].split('def _voice_fx_page(self)', 1)[0]
    assert 'MicroProcessingDialog(' in tune_block
    assert 'embedded=True' in tune_block
    assert 'activity_callback=self._backend_controller.micro_processing_activity' in tune_block
