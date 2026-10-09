from __future__ import annotations

from pathlib import Path

from ksound_hub.shortcuts import normalize_shortcut_trigger, shortcut_trigger_is_reserved


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_reserved_editor_keys_cannot_be_shortcuts():
    for key in (
        "Return", "Enter", "Backspace", "Delete", "Escape", "Tab",
        "Ctrl+Backspace", "Shift", "Ctrl", "Alt", "Meta",
        "CapsLock", "NumLock", "ScrollLock",
    ):
        assert shortcut_trigger_is_reserved(key)
        assert normalize_shortcut_trigger(key) == ""
    assert not shortcut_trigger_is_reserved("F22")
    assert normalize_shortcut_trigger("Ctrl+B") == "Ctrl+B"


def test_mixer_registers_channel_shortcuts_for_every_channel():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert 'MIXER_SHORTCUT_ACTIONS = (' in source
    assert '("mute", "Mute")' in source
    assert '("voldown", "Volume Down")' in source
    assert '("volup", "Volume Up")' in source
    assert '("device_next", "Next favorite device")' in source
    assert 'module="mixer"' in source
    assert 'mixer_shortcut_id(channel_key, action)' in source


def test_channel_stereo_popover_contains_compact_shortcut_rows():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert 'shortcuts_title = QLabel("Shortcuts", popover)' in source
    assert '("mute", "Mute")' in source
    assert '("voldown", "Vol −")' in source
    assert '("volup", "Vol +")' in source
    assert '("device_next", "Next ★")' in source
    assert 'stereoPopoverShortcutEdit' in source
    assert 'stereoPopoverShortcutCheck' in source


def test_recorder_semantics_are_direct_one_press_clear_validate_cancel():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert 'if key in {Qt.Key_Return, Qt.Key_Enter}' in source
    assert 'if key in {Qt.Key_Backspace, Qt.Key_Delete}' in source
    assert 'if key == Qt.Key_Escape' in source
    assert 'event.keyCombination()' in source
    assert 'self.keySequenceChanged.connect' not in source
    assert 'Qt.Key_CapsLock' in source
    assert 'Qt.Key_NumLock' in source
    assert 'Qt.Key_ScrollLock' in source
    assert 'self.shortcutCommitted.emit(normalized)' in source
    assert 'QTimer.singleShot(0, self.clearFocus)' in source


def test_volume_shortcuts_use_single_hot_path_and_background_audio_worker():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    audio = (_root() / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
    overlay = (_root() / "src/ksound_hub/ui/overlay.py").read_text(encoding="utf-8")
    assert 'def render_shortcut_volume(self, value: int)' in source
    assert 'card.sync_from_saved_state(volume=target, muted=muted' not in source
    assert 'self._realtime_volume_thread = threading.Thread' in source
    assert 'name="ksounds-volume-apply"' in source
    assert 'def _queue_realtime_volume_apply' in source
    assert 'def _realtime_volume_worker' in source
    assert 'self._queue_realtime_volume_apply(channel.key, final=final)' in source
    assert 'self._pending_apply_keys.add(channel.key)' not in source[source.index('def _set_mixer_shortcut_volume_target'):source.index('def _apply_mixer_action')]
    assert 'self._mixer_volume_hold_timer.setInterval(16)' in source
    assert '5.0 * direction' in source
    assert '58.0 * dt * direction' in source
    assert 'min(0.020, now - previous)' in source
    assert 'show_overlay=True' in source
    assert 'threading.RLock()' in audio
    assert 'def apply_channel_volume_realtime' in audio
    assert 'threading.Thread' in overlay
    assert 'self._writer_pending = payload' in overlay
    assert 'final=True' in source


def test_shortcut_fields_are_more_visible_in_stereo_popover():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert 'background: rgba(255, 255, 255, 34);' in source
    assert 'border: 1px solid rgba(190, 225, 245, 90);' in source
    assert 'QKeySequenceEdit#stereoPopoverShortcutEdit:hover' in source
    assert 'QCheckBox#stereoPopoverShortcutCheck::indicator:checked' in source


def test_favorite_device_switching_is_persisted_and_exposed_in_cards():
    models = (_root() / "src/ksound_hub/models.py").read_text(encoding="utf-8")
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert "favorite_output_targets" in models
    assert "favorite_input_targets" in models
    assert "compactSelectFavorite" in source
    assert 'QPushButton("★", row)' in source
    assert 'color: rgba(92, 105, 116, 165);' in source
    assert 'color: rgba(255, 221, 102, 255);' in source
    assert 'add_section_header("Favorites", favorites=True)' in source
    assert 'favorite_other_label: str = "Other devices"' in source
    assert 'add_section_header(self._favorite_other_label)' in source
    assert "Choose device. Use ★ inside the menu to add/remove favorites." in source
    assert "favorite_set = set(favorite_items)" in source
    assert "other_items = [item for item in available_items if item not in favorite_set]" in source
    assert "if len(other_items) > 4:" in source
    assert "add_scrolling_device_rows(other_items)" in source
    assert 'scroll.setFixedHeight(max(31, visible_rows * 31 + 2))' in source
    assert 'QScrollBar#compactSelectScrollBar:vertical' in source
    assert "deviceFavoriteButton" not in source
    assert "def _cycle_channel_favorite_device" in source
    assert "def set_favorite_device_target" in source
    assert "def favorite_device_targets" in source
    assert 'action == "device_next"' in source
