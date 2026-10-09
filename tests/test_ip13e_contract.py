from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
TUNE = (ROOT / 'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
VFX = (ROOT / 'src/ksound_hub/voice_fx_ui.py').read_text(encoding='utf-8')


def test_ip13e_build_marker_and_sidebar_polish():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'registry.navigation_modules()' in GLASS and '_rebuild_navigation' in GLASS
    assert 'button = NavButton(icon, label)' in GLASS
    assert 'self.fx_btn = QPushButton("Tune", self)' in GLASS
    assert 'self.setMinimumHeight(44)' in GLASS


def test_vfx_page_has_soundboard_style_monitoring_card_and_open_action():
    block = GLASS.split('def _voice_fx_page(self)', 1)[1].split('def _sync_voice_fx_monitoring', 1)[0]
    assert 'PermanentRouteCard(' in block
    assert '"Monitoring"' in block
    assert 'self.voice_fx_monitor_active = QLabel("Off")' in block
    assert 'open_button = QPushButton("Open")' in block
    assert 'open_button.clicked.connect(self._detach_voice_fx)' in block
    assert 'scroll = self._make_scroll_page(content)' in block
    assert 'scroll.set_content_width_locked(True)' in block
    assert 'return scroll' in block


def test_vfx_monitoring_tracks_central_state_and_detached_surface():
    assert 'def _sync_voice_fx_monitoring(self, state: dict)' in GLASS
    assert 'full_active_text = " + ".join' in GLASS
    assert 'strength.setText(f"Strength {amount}%")' in GLASS
    assert 'class DetachedVoiceFxWindow(QMainWindow):' in GLASS
    assert 'backend_controller.micro_voice_fx_state_changed.connect(self.sync_state)' in GLASS
    assert 'self._sync_voice_fx_monitoring(state)' in GLASS


def test_embedded_tune_is_compacter_and_uses_human_case_name():
    assert 'Tune · MICRO Processing' in TUNE
    assert 'if self._embedded' in TUNE
    assert 'QDialog#microProcessingSurface[embedded="true"]' in TUNE


def test_embedded_vfx_is_compacter_without_changing_effect_model():
    assert 'if not self._embedded:' in VFX
    assert 'title = QLabel("Voice FX · native low-latency")' in VFX
    assert 'QDialog#voiceFxDialog[embedded="true"]' in VFX
    assert 'Radio/Telephone' in VFX
    assert 'Pitch ↑/↓ and the room effects replace their alternatives.' in VFX
    assert 'self._apply({"active": list(self._active)})' in VFX
