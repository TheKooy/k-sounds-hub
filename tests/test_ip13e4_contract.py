from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")


def test_ip13e4_build_marker_family():
    assert 'APP_BUILD = "IP' in GLASS


def test_vfx_scroll_page_is_hard_locked_to_drawer_viewport():
    adaptive = GLASS.split("class AdaptiveScrollArea", 1)[1].split("class NoWheelComboBox", 1)[0]
    assert "self._lock_content_width = False" in adaptive
    assert "def set_content_width_locked" in adaptive
    assert "widget.setMaximumWidth(viewport_width)" in adaptive
    page = GLASS.split("def _voice_fx_page(self)", 1)[1].split("def _sync_voice_fx_monitoring", 1)[0]
    assert "content.setMinimumWidth(0)" in page
    assert "content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)" in page
    assert "monitoring_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)" in page
    assert "editor_shell.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)" in page
    assert "scroll.set_content_width_locked(True)" in page


def test_embedded_vfx_controls_ignore_long_text_width_hints():
    assert "self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)" in VFX
    assert "button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)" in VFX
    assert "if not self._embedded:" in VFX
    assert "amount_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)" in VFX
