from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    left = GLASS.index(start)
    right = GLASS.index(end, left)
    return GLASS[left:right]


def test_pad_shortcut_ids_are_stable_and_namespaced():
    block = _block("def soundboard_pad_shortcut_id", "SOUNDBOARD_AUTO_PALETTES")
    assert 'hashlib.sha256' in block
    assert 'hexdigest()[:20]' in block
    assert 'soundboard.pad.' in block


def test_soundboard_pads_join_the_central_registry_before_saved_bindings_are_applied():
    block = _block("    def _register_core_shortcuts", "    def _tune_shortcut_binding")
    assert "self._register_soundboard_shortcuts()" in block
    assert 'module="soundboard"' not in block  # registration is delegated, not duplicated here
    assert block.index("self._register_soundboard_shortcuts()") < block.index("apply_saved_bindings")

    register = _block("    def _ensure_soundboard_shortcut_action", "    def _register_soundboard_shortcuts")
    assert 'module="soundboard"' in register
    assert "ShortcutBinding(" in register
    assert "self.shortcut_registry.register(" in register


def test_global_backend_registers_mixer_and_soundboard_actions():
    block = _block("    def _start_global_mixer_shortcuts", "    def _start_global_shortcuts")
    assert 'binding.module not in {"mixer", "soundboard", "micro_preset"}' in block
    assert "self.shortcut_manager.activated(shortcut)" in block
    assert "self.shortcut_manager.deactivated(shortcut)" in block


def test_pad_editor_is_contextual_and_uses_the_same_recorder_widget():
    card = _block("class SoundPadCard", "class PadsPanel")
    assert 'QPushButton("⌨")' in card
    assert 'setToolTip("Edit global shortcut")' in card
    assert "ShortcutKeyEdit(popover)" in card
    assert "Backspace/Delete clears; Enter validates; Esc cancels." in card
    assert "request_soundboard_pad_shortcut_update" not in card  # card remains callback-driven


def test_detached_pad_ui_reads_and_writes_through_backend_controller():
    add_pad = _block("    def _add_pad(self, name:", "    def _reflow_grid")
    assert 'getattr(self.backend_controller, "soundboard_pad_shortcut_state", None)' in add_pad
    assert 'getattr(self.backend_controller, "request_soundboard_pad_shortcut_update", None)' in add_pad

    controller = _block("    def soundboard_pad_shortcut_state", "    def set_soundboard_micro_enabled")
    assert "_soundboard_shortcut_state_resolver" in controller
    assert "soundboard_shortcut_edit_requested.emit" in controller
    assert "soundboard_shortcut_state_changed.emit" in controller


def test_global_pad_activation_uses_soundboard_service_without_route_rebuild():
    block = _block("    def play_soundboard_slot_shortcut", "    def set_soundboard_micro_enabled")
    assert "self.soundboard_service.play_slot(slot)" in block
    assert "_load_soundboard_output_loopback" not in block
    assert "_load_soundboard_micro_loopback" not in block
    assert "overlay_message_requested.emit" in block
