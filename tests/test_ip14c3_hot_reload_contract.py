from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
PIPEWIRE = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")


def test_lab_parameter_changes_refresh_native_state_without_runtime_restart():
    block = GLASS.split("def set_micro_custom_fx_state", 1)[1].split("def micro_hard_gate_enabled", 1)[0]
    assert "self._refresh_native_micro_hard_gate_runtime()" in block
    assert "restart_module_runtime" not in block
    assert "runtime_supervisor.restart" not in block
    assert "self.settings_store.save(self.settings)" in block


def test_disabling_voice_fx_also_bypasses_custom_chain_in_runtime_copy():
    block = GLASS.split("def _audio_runtime_settings", 1)[1].split("def apply_startup_settings", 1)[0]
    assert "channel.micro_custom_fx_enabled = False" in block


def test_native_micro_state_is_atomic_hot_reload_path():
    assert "def refresh_native_micro_state" in PIPEWIRE
    assert "tmp.replace(self._native_micro_state_path)" in PIPEWIRE
    assert "custom_fx_module" in PIPEWIRE
