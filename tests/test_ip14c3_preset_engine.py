from __future__ import annotations

from pathlib import Path

import pytest

from ksound_hub.dsp_registry import DspModuleRegistry
from ksound_hub.models import ChannelConfig
from ksound_hub.preset_engine import PresetEngine, PresetValidationError


ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
PIPEWIRE = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
MICRO = (ROOT / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")
VOICE_FX_DSP = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")


def _sample_preset(engine: PresetEngine) -> dict:
    preset = engine.new_preset("Kooy Modular Test", "vfx")
    preset["wet_percent"] = 78
    preset["modules"] = [
        {"type": "highpass", "params": {"frequency_hz": 180}},
        {"type": "lowpass", "params": {"frequency_hz": 5200}},
        {"type": "saturation", "params": {"drive": 2.4}},
        {"type": "delay", "params": {"delay_ms": 70, "feedback_percent": 18, "mix_percent": 24}},
    ]
    return preset


def test_ip14c3_build_and_preset_lab_are_wired():
    assert 'APP_BUILD = "IP' in GLASS
    assert "self.dsp_module_registry = DspModuleRegistry.default()" in GLASS
    assert "self.preset_engine = PresetEngine(self.dsp_module_registry)" in GLASS
    assert "def micro_custom_fx_state" in GLASS
    assert "def set_micro_custom_fx_state" in GLASS
    assert 'lab_button = QPushButton("Lab")' in GLASS
    assert "PresetLabDialog(" in GLASS


def test_registry_separates_conceptual_targets_from_live_runtime_support():
    registry = DspModuleRegistry.default()
    live_vfx = [spec.module_type for spec in registry.for_target("vfx", runtime_only=True)]
    assert live_vfx[:5] == ["gain", "highpass", "lowpass", "saturation", "delay"]
    assert live_vfx[5:] == ["distortion", "pitch", "reverb"]
    assert "input" in registry.get("gain").allowed_targets
    assert "output" in registry.get("gain").allowed_targets
    assert registry.get("hard_gate").allowed_targets == ("input",)
    assert registry.get("delay").allowed_targets == ("output", "vfx")
    assert registry.get("delay").runtime_targets == ("vfx",)


def test_preset_engine_validates_targets_duplicates_and_parameters(tmp_path: Path):
    engine = PresetEngine(DspModuleRegistry.default(), tmp_path)
    preset = _sample_preset(engine)
    normalized = engine.runtime_chain(preset, "vfx")
    assert normalized["format"] == "ksounds-preset"
    assert normalized["format_version"] == 1
    assert normalized["wet_percent"] == 78
    assert [item["type"] for item in normalized["modules"]] == ["highpass", "lowpass", "saturation", "delay"]

    bad_target = engine.new_preset("Bad input delay", "input")
    bad_target["modules"] = [{"type": "delay", "params": {}}]
    with pytest.raises(PresetValidationError):
        engine.normalize(bad_target, target="input")

    too_many = engine.new_preset("Too many gains", "vfx")
    too_many["modules"] = [{"type": "gain", "params": {}} for _ in range(3)]
    with pytest.raises(PresetValidationError):
        engine.runtime_chain(too_many, "vfx")


def test_kspreset_save_load_export_import_round_trip(tmp_path: Path):
    library = tmp_path / "library"
    engine = PresetEngine(DspModuleRegistry.default(), library)
    preset = engine.runtime_chain(_sample_preset(engine), "vfx")
    saved = engine.save(preset)
    assert saved.suffix == ".kspreset"
    assert engine.load(saved, target="vfx", runtime=True) == preset

    exported = engine.export_file(preset, tmp_path / "shared-kooy-preset")
    assert exported.name.endswith(".kspreset")
    imported_path, imported = engine.import_file(exported)
    assert imported == preset
    assert imported_path.is_file()


def test_channel_settings_preserve_portable_custom_chain(tmp_path: Path):
    engine = PresetEngine(DspModuleRegistry.default(), tmp_path)
    preset = engine.runtime_chain(_sample_preset(engine), "vfx")
    channel = ChannelConfig(key="micro", name="MICRO")
    channel.micro_custom_fx_enabled = True
    channel.micro_custom_fx_preset = preset
    restored = ChannelConfig.from_dict(channel.to_dict())
    assert restored.micro_custom_fx_enabled is True
    assert restored.micro_custom_fx_preset == preset


def test_native_state_writer_and_engine_have_generic_chain_protocol():
    assert '"version\\t10"' in PIPEWIRE
    assert 'custom_fx_enabled\\t' in PIPEWIRE
    assert 'custom_fx_wet\\t' in PIPEWIRE
    assert 'custom_fx_module\\t' in PIPEWIRE
    assert 'custom_fx_module\\tdelay' in PIPEWIRE

    assert 'struct CustomFxModuleDsp' in VOICE_FX_DSP
    assert 'void apply_custom_fx' in VOICE_FX_DSP
    assert 'arg == "--probe-custom-fx"' in MICRO
    # Do not pin the C++ source to one literal probe-output string. The staged
    # installer executes --probe-custom-fx and requires custom_fx=ok at runtime.
    assert 'if (probe_custom_fx)' in MICRO
    assert 'dsp.process_custom(frames, probe_state.custom_fx_enabled, probe_state.custom_fx_wet, probe_state.custom_fx_modules);' in MICRO
    assert MICRO.index('micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);') < MICRO.index('micro_custom_fx.process_custom(mic_chunk, state.custom_fx_enabled, state.custom_fx_wet, state.custom_fx_modules);')
