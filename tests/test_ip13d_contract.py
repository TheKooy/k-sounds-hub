from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
TUNE = (ROOT / 'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
VFX = (ROOT / 'src/ksound_hub/voice_fx_ui.py').read_text(encoding='utf-8')


def test_ip13d_build_marker_and_navigation():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'registry.navigation_modules()' in GLASS and '_rebuild_navigation' in GLASS
    assert 'button = NavButton(icon, label)' in GLASS
    assert 'self._add_page("micro_tune", self._tune_page())' in GLASS
    assert 'self._add_page("voice_fx", self._voice_fx_page())' in GLASS
    assert ('self.setMinimumHeight(46)' in GLASS or 'self.setMinimumHeight(44)' in GLASS)


def test_card_buttons_open_first_class_pages_not_modal_dialogs():
    assert 'self._show_drawer_nav_page(3)' in GLASS
    assert 'self._show_drawer_nav_page(4)' in GLASS
    tune_block = GLASS.split('def _open_micro_processing_dialog', 1)[1].split('def _sync_micro_voice_fx_card_state', 1)[0]
    vfx_block = GLASS.split('def _open_micro_voice_fx_dialog', 1)[1].split('def _set_micro_hard_gate_enabled', 1)[0]
    assert '.exec()' not in tune_block
    assert '.exec()' not in vfx_block


def test_tune_surface_supports_embedded_mode_and_state_sync():
    assert 'embedded: bool = False' in TUNE
    assert 'self.setWindowFlags(Qt.Widget)' in TUNE
    assert 'def sync_state(self, state: dict)' in TUNE
    assert 'QDialog#microProcessingSurface[embedded="true"] { background: transparent; }' in TUNE
    assert 'if not self._embedded:' in TUNE
    assert 'self.eq_name' in TUNE
    assert '_ksound_value_label' in TUNE


def test_vfx_surface_supports_embedded_mode_and_state_sync():
    assert 'embedded: bool = False' in VFX
    assert 'self.setWindowFlags(Qt.Widget)' in VFX
    assert 'def sync_state(self, state: dict)' in VFX
    assert 'QDialog#voiceFxDialog[embedded="true"]' in VFX
    assert 'if not self._embedded:' in VFX


def test_central_state_fans_out_to_pages():
    assert 'micro_processing_state_changed = Signal()' in GLASS
    assert 'self.micro_processing_state_changed.emit()' in GLASS
    assert 'self.backend_controller.micro_processing_state_changed.connect(self.drawer.sync_micro_processing_state)' in GLASS
    assert 'lambda _active: self.drawer.sync_voice_fx_state()' in GLASS
    assert 'sync(self._backend_controller.micro_processing_state())' in GLASS
    assert ('sync(self._backend_controller.micro_voice_fx_state())' in GLASS or 'state = self._backend_controller.micro_voice_fx_state()' in GLASS)


def test_drawer_page_indexes_are_updated_for_tune_vfx():
    assert 'if route == "micro_tune":' in GLASS
    assert 'self.sync_micro_processing_state()' in GLASS
    assert 'elif route == "voice_fx":' in GLASS
    assert 'self.sync_voice_fx_state()' in GLASS
    assert 'elif route == "settings":' in GLASS
    assert 'elif route == "pads":' in GLASS
