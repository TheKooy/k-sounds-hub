from __future__ import annotations

from pathlib import Path

from ksound_hub.dsp_registry import DspModuleRegistry
from ksound_hub.preset_engine import PresetEngine

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
LAB = (ROOT / "src/ksound_hub/preset_lab_ui.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")


def test_ip14c6_build_and_custom_effect_library_are_wired():
    assert 'APP_BUILD = "IP' in GLASS
    assert "def voice_fx_user_presets" in GLASS
    assert 'self.preset_engine.list_for_target("vfx", runtime=True)' in GLASS
    assert "custom_presets_provider=self._backend_controller.voice_fx_user_presets" in GLASS
    assert "custom_apply_callback=self._backend_controller.set_micro_custom_fx_state" in GLASS
    assert "library_changed_callback=self._refresh_custom_voice_fx_library" in GLASS


def test_preset_lab_wheel_does_not_edit_unfocused_values():
    assert "class ClickFocusedDoubleSpinBox" in LAB
    assert "class ClickFocusedSlider" in LAB
    assert "if not self.hasFocus():" in LAB
    assert "event.ignore()" in LAB
    assert "editor = ClickFocusedDoubleSpinBox()" in LAB
    assert "self.wet_slider = ClickFocusedSlider(Qt.Horizontal)" in LAB


def test_saved_vfx_presets_are_discoverable_as_runtime_effects(tmp_path: Path):
    engine = PresetEngine(DspModuleRegistry.default(), tmp_path)
    preset = engine.new_preset("Shared Kooy FX", "vfx")
    preset["modules"] = [{"type": "gain", "params": {"gain_db": 2.0}}]
    engine.save(preset)

    other = engine.new_preset("Output only", "output")
    other["modules"] = [{"type": "gain", "params": {"gain_db": 1.0}}]
    engine.save(other)

    found = engine.list_for_target("vfx", runtime=True)
    assert [item[1]["name"] for item in found] == ["Shared Kooy FX"]


def test_voice_fx_surface_supports_saved_custom_effect_buttons():
    assert 'self.custom_label = QLabel("Custom presets")' in VFX
    assert "def refresh_custom_presets" in VFX
    assert "def sync_custom_state" in VFX
    assert "def _on_custom_effect_clicked" in VFX
    assert 'callback({"enabled": bool(enabled), "preset": preset})' in VFX
