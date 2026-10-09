from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_micro_processing_dialog_exposes_ui_first_controls():
    text = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
    for marker in (
        'VisibleCheckBox("Processing")',
        'QPushButton("Bypass now")',
        '1 · Input Gain',
        '2 · Noise Suppression',
        '3 · Gate',
        '4 · MICRO EQ',
        '5 · Compressor',
        '6 · De-esser',
        '7 · Limiter',
        'RNNoise backend:',
    ):
        assert marker in text


def test_glass_micro_processing_backend_exposes_soft_gate_hard_gate_and_eq_profile():
    text = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert '"gate_enabled"' in text
    assert '"gate_threshold"' in text
    assert '"hard_gate_enabled"' in text
    assert '"hard_gate_threshold"' in text
    assert '"eq_profile"' in text
    assert 'self._sync_micro_hard_gate_audio_runtime()' in text


def test_micro_processing_scrollbar_and_slider_wheel_passthrough():
    text = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
    assert "class ScrollPassthroughSlider(QSlider):" in text
    assert "event.ignore()" in text
    assert "scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in text
    assert "scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)" in text
    assert "slider = ScrollPassthroughSlider(Qt.Horizontal)" in text
    assert "QScrollBar:vertical" in text


def test_disabled_effect_cards_are_visually_dimmed_without_losing_controls():
    text = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
    assert "QGraphicsOpacityEffect" in text
    assert "def _set_effect_card_enabled" in text
    assert "def _sync_effect_card_visuals" in text
    assert "effect.setOpacity(1.0 if bool(enabled) else 0.46)" in text
    assert "keep the checkbox and sliders interactive" in text
    for marker in (
        "self._noise_box",
        "self._gate_box",
        "self._compressor_box",
        "self._deesser_box",
        "self._limiter_box",
    ):
        assert marker in text


def test_processing_checkboxes_have_visible_squares():
    text = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
    assert "class VisibleCheckBox(QCheckBox):" in text
    assert "SE_CheckBoxIndicator" in text
    assert "painter.drawRoundedRect" in text
    assert text.count("VisibleCheckBox(") >= 6


def test_ab_processing_and_master_bypass_visual_state():
    text = (ROOT / "src/ksound_hub/micro_processing_ui.py").read_text(encoding="utf-8")
    assert 'QPushButton("A/B Processing")' in text
    assert 'QKeySequence("Ctrl+B")' not in text
    assert "lambda: self.master_toggle.setChecked(not self.master_toggle.isChecked())" in text
    assert "master_active=master_active" in text
    assert 'VisibleCheckBox("Enable Gate")' in text
    assert "card-level Hard Gate is separate" in text


def test_micro_eq_is_directly_accessible():
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'QPushButton("Edit MICRO EQ")' in ui
    assert '("MICRO", "micro")' in glass
    assert 'def select_channel(self, channel_key: str)' in glass
    assert 'def _open_micro_eq_editor(self)' in glass

def test_ab_shortcut_is_configurable_in_settings():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    models=(ROOT/'src/ksound_hub/models.py').read_text(encoding='utf-8')
    assert "QKeySequenceEdit" in glass
    assert 'micro_processing_ab_shortcut: str = "Ctrl+B"' in models
    assert 'if key == "micro_processing_ab_shortcut":' in glass
    assert "def _toggle_micro_processing_ab(self)" in glass


def test_micro_eq_is_isolated_from_playback_eq_library():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    eq_block=glass[glass.index('class EqPanel(QWidget):'):glass.index('class CenterGlyphButton', glass.index('class EqPanel(QWidget):'))]
    assert '("MICRO", "micro")' not in eq_block.split('def __init__', 1)[0]
    assert 'channels=[("MICRO", "micro")]' in glass
    assert 'show_channel_selector=False' in glass
    assert 'Playback outputs intentionally share one EQ library' in glass
    assert 'Dedicated microphone EQ' in ui

def test_ab_shortcut_uses_application_event_filter():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'self._micro_processing_ab_sequence = QKeySequence()' in glass
    assert 'def _micro_processing_ab_event_matches' in glass
    assert 'event.type() == QEvent.KeyPress' in glass
    assert '_micro_processing_ab_shortcut = QShortcut' not in glass


def test_ab_shortcut_uses_qt6_key_combination():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'event.keyCombination()' in glass
    assert 'app.installEventFilter(self)' in glass
    assert 'QKeySequence.PortableText' in glass


def test_ab_shortcut_can_be_disabled_without_erasing_chord():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    models=(ROOT/'src/ksound_hub/models.py').read_text(encoding='utf-8')
    assert 'micro_processing_ab_shortcut_enabled: bool = True' in models
    assert 'VisibleCheckBox("Enable in K-Sounds")' in glass
    assert '"micro_processing_ab_shortcut_enabled"' in glass
    assert 'micro_processing_ab_shortcut_edit.setEnabled' in glass
    assert 'QKeySequence(raw) if binding.enabled and raw else QKeySequence()' in glass


def test_micro_processing_dialog_shows_live_native_reduction_activity():
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'activity_callback: Callable[[], dict] | None = None' in ui
    assert 'self._activity_timer.setInterval(100)' in ui
    assert 'Reduction: —' in ui
    assert 'Sibilance reduction: —' in ui
    assert 'Peak reduction: —' in ui
    assert 'def _poll_activity(self)' in ui
    assert 'def micro_processing_activity(self) -> dict' in glass
    assert 'activity_callback=self._backend_controller.micro_processing_activity' in glass


def test_ip11g3_uses_visible_graphical_reduction_meters():
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    assert "QProgressBar" in ui
    assert 'microProcessingReductionMeter' in ui
    assert 'self.compressor_meter = self._make_reduction_meter' in ui
    assert 'self.deesser_meter = self._make_reduction_meter' in ui
    assert 'self.limiter_meter = self._make_reduction_meter' in ui
    assert 'def _set_activity_meter' in ui
    assert 'reduction * 10.0' in ui


def test_limiter_has_adjustable_ceiling():
    ui=(ROOT/'src/ksound_hub/micro_processing_ui.py').read_text(encoding='utf-8')
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'VisibleCheckBox("Enable Safety Limiter")' in ui
    assert 'self.limiter_ceiling = self._slider_row' in ui
    assert '"limiter_ceiling_db"' in ui
    assert '"limiter_ceiling_db"' in glass


def test_ip12e2_global_tune_structural_integration():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'def _start_global_tune_shortcut' in glass
    assert 'def _stop_global_tune_shortcut' in glass
    assert 'def _restart_global_tune_shortcut' in glass
    assert '_global_tune_shortcut_active' in glass
    assert 'Global TUNE shortcut available' in glass
    assert 'using in-app fallback' in glass
    assert 'bridge = getattr(self, "_global_tune_shortcut", None)' in glass
    assert 'if bool(getattr(bridge, "portal_ready", False)):' in glass


def test_ip12e3_global_tune_guard_tracks_live_helper_state():
    glass=(ROOT/'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    assert 'bridge = getattr(self, "_global_tune_shortcut", None)' in glass
    assert 'if bool(getattr(bridge, "portal_ready", False)):' in glass


def test_ip12e4_global_tune_consent_is_async():
    bridge=(ROOT/'src/ksound_hub/shortcuts_portal_bind.py').read_text(encoding='utf-8')
    assert 'waiting for KDE Global Shortcuts confirmation' in bridge
    assert 'timer.start(20000)' not in bridge
    assert 'loop.exec()' not in bridge
    assert 'if event == "PENDING":' in bridge
