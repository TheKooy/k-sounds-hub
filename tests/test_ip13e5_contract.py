from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def test_ip13e5_build_marker_survives_later_versions():
    assert 'APP_BUILD = "IP' in GLASS


def test_ip13e5_vfx_page_uses_locked_scroll_viewport():
    block = GLASS.split('def _voice_fx_page(self)', 1)[1].split('def _sync_voice_fx_monitoring', 1)[0]
    assert 'scroll = self._make_scroll_page(content)' in block
    assert 'scroll.set_content_width_locked(True)' in block
    assert 'return scroll' in block
