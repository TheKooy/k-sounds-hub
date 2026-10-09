from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
TUNE = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")


def test_ip13e3_build_and_sidebar_symbols_are_visually_stronger():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'registry.navigation_modules()' in GLASS and '_rebuild_navigation' in GLASS
    assert 'button = NavButton(icon, label)' in GLASS


def test_tune_embedded_scale_is_reduced_for_drawer_density():
    assert 'outer.setSpacing(5 if self._embedded else 10)' in TUNE
    assert 'master_layout.setContentsMargins(*( (8, 5, 8, 5) if self._embedded else' in TUNE
    assert 'layout.setContentsMargins(*( (8, 5, 8, 5) if self._embedded else' in TUNE
    assert 'font-size: 15px;' in TUNE
    assert 'min-height: 21px;' in TUNE
    assert 'meter.setFixedHeight(6 if self._embedded else 8)' in TUNE


def test_vfx_editor_is_contained_and_one_column_at_drawer_width():
    page = GLASS.split('def _voice_fx_page(self)', 1)[1].split('def _sync_voice_fx_monitoring', 1)[0]
    assert 'editor_shell = QFrame()' in page
    assert 'editor_shell.setObjectName("voiceFxEditorShell")' in page
    assert 'editor_layout.setContentsMargins(8, 8, 8, 8)' in page
    assert 'root.addWidget(editor_shell)' in page
    assert 'if self._embedded:' in VFX
    assert 'grid.addWidget(button, index, 0)' in VFX
    assert 'max-height: 52px;' in VFX
