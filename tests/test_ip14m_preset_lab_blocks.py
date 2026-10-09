from __future__ import annotations

import json
from pathlib import Path

from ksound_hub.dsp_registry import DspModuleRegistry
from ksound_hub.preset_engine import PresetEngine, PresetValidationError


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _new_chain(engine: PresetEngine) -> dict:
    preset = engine.new_preset("Kooy Monster Lab", "vfx")
    preset["wet_percent"] = 82
    preset["modules"] = [
        {"type": "highpass", "params": {"frequency_hz": 100}},
        {"type": "distortion", "params": {"drive": 9.5, "mix_percent": 65}},
        {"type": "pitch", "params": {"semitones": -8.0, "mix_percent": 95}},
        {"type": "reverb", "params": {"predelay_ms": 28, "decay_percent": 72, "mix_percent": 42}},
        {"type": "lowpass", "params": {"frequency_hz": 6200}},
    ]
    return preset


def test_live_lab_catalog_exposes_three_real_processors() -> None:
    registry = DspModuleRegistry.default()
    live = [spec.module_type for spec in registry.for_target("vfx", runtime_only=True)]
    assert live == ["gain", "highpass", "lowpass", "saturation", "delay", "distortion", "pitch", "reverb"]
    assert registry.get("pitch").max_instances == 2
    assert registry.get("reverb").max_instances == 2
    assert "vfx" in registry.get("distortion").runtime_targets
    assert "vfx" in registry.get("pitch").runtime_targets
    assert "vfx" in registry.get("reverb").runtime_targets
    # Declared but unavailable processors must not suddenly appear in the live Lab.
    assert "parametric_eq" not in live
    assert "compressor" not in live


def test_new_blocks_are_ordered_portable_and_editable(tmp_path: Path) -> None:
    engine = PresetEngine(DspModuleRegistry.default(), tmp_path)
    chain = engine.runtime_chain(_new_chain(engine), "vfx")
    assert [item["type"] for item in chain["modules"]] == [
        "highpass", "distortion", "pitch", "reverb", "lowpass"
    ]
    assert chain["modules"][2]["params"] == {"semitones": -8.0, "mix_percent": 95.0}
    assert chain["modules"][3]["params"] == {
        "predelay_ms": 28.0, "decay_percent": 72.0, "mix_percent": 42.0
    }
    saved = engine.save(chain)
    assert engine.load(saved, target="vfx", runtime=True) == chain
    assert [item[1]["name"] for item in engine.list_for_target("vfx", runtime=True)] == ["Kooy Monster Lab"]
    exported = engine.export_file(chain, tmp_path / "share-this")
    assert engine.load(exported, target="vfx", runtime=True) == chain
    assert json.loads(exported.read_text(encoding="utf-8"))["format_version"] == 1


def test_lab_bounds_are_safe_and_mix_can_bypass(tmp_path: Path) -> None:
    engine = PresetEngine(DspModuleRegistry.default(), tmp_path)
    chain = _new_chain(engine)
    chain["modules"][1]["params"] = {"drive": 99, "mix_percent": 0}
    chain["modules"][2]["params"] = {"semitones": float("nan"), "mix_percent": -20}
    chain["modules"][3]["params"] = {"predelay_ms": 200, "decay_percent": -10, "mix_percent": 0}
    clean = engine.runtime_chain(chain, "vfx")
    assert clean["modules"][1]["params"] == {"drive": 20.0, "mix_percent": 0.0}
    assert clean["modules"][2]["params"] == {"semitones": -5.0, "mix_percent": 0.0}
    assert clean["modules"][3]["params"] == {
        "predelay_ms": 100.0, "decay_percent": 0.0, "mix_percent": 0.0
    }
    chain["modules"] = [{"type": "pitch", "params": {}}] * 3
    try:
        engine.runtime_chain(chain, "vfx")
    except PresetValidationError as exc:
        assert "Too many Pitch" in str(exc)
    else:
        raise AssertionError("The max-instances guard must remain active")


def test_end_to_end_writer_parser_and_ordered_native_dsp_contract() -> None:
    writer = _read("src/ksound_hub/audio/pipewire_v2_final.py")
    native = _read("native_engine/src/micro_engine.cpp")
    dsp = _read("native_engine/src/voice_fx_dsp.cpp")
    lab = _read("src/ksound_hub/preset_lab_ui.py")
    for marker in (
        'custom_fx_module\\tdistortion',
        'custom_fx_module\\tpitch',
        'custom_fx_module\\treverb',
    ):
        assert marker in writer
    for marker in ('spec.type == "distortion"', 'spec.type == "pitch"', 'spec.type == "reverb"'):
        assert marker in native
        assert marker in dsp
    assert 'CustomFxModuleDsp' in dsp
    assert 'const auto& spec = state.custom_fx_modules[module_index];' in dsp
    assert 'module.fun_dsp' in dsp
    assert 'self.registry.for_target("vfx", runtime_only=True)' in lab
    assert 'editor = ClickFocusedDoubleSpinBox()' in lab
    assert 'self._move_module(i, -1)' in lab and 'self._move_module(i, 1)' in lab
    assert 'description = QLabel(spec.description)' in lab
    assert '"format_version": PRESET_FORMAT_VERSION' in _read("src/ksound_hub/preset_engine.py")


def test_release_changes_only_voice_fx_module_semver() -> None:
    assert 'APP_BUILD = "IP' in _read("src/ksound_hub/glass_app.py")
    assert 'Hub IP14' in _read("tools/soundboard_remote/ksound_soundboard_web.py")
    manifest = json.loads(_read("src/ksound_hub/builtin_modules/voice_fx/module.json"))
    assert manifest["version"] == "1.2.0"
    assert manifest["ksounds_api"] == 1
    assert "native_engine/src/voice_fx_dsp.cpp" in manifest["owned_paths"]
    assert "src/ksound_hub/preset_lab_ui.py" in manifest["owned_paths"]


def test_writer_emits_ordered_creative_modules_for_the_actual_native_engine(tmp_path: Path) -> None:
    from ksound_hub.audio.pipewire_v2_final import PipeWireAudioEngine
    from ksound_hub.models import AppSettings

    presets = PresetEngine(DspModuleRegistry.default(), tmp_path)
    settings = AppSettings.default()
    micro = next(channel for channel in settings.channels if channel.key == "micro")
    micro.micro_custom_fx_enabled = True
    micro.micro_custom_fx_preset = presets.runtime_chain(_new_chain(presets), "vfx")

    backend = object.__new__(PipeWireAudioEngine)
    backend._find_channel = lambda loaded, key: next(
        (channel for channel in loaded.channels if channel.key == key), None
    )
    backend._native_micro_source_for_channel = lambda _channel: "test-physical-mic"
    backend._current_profile = lambda _channel: None
    backend._bands_spec = lambda _profile: ""
    backend._native_micro_soundboard_send_to_micro = lambda: False
    text = PipeWireAudioEngine._render_native_micro_state_text(backend, settings)
    lines = [line for line in text.splitlines() if line.startswith("custom_fx_module\t")]
    assert lines == [
        "custom_fx_module\thighpass\t100.0000\t0\t0",
        "custom_fx_module\tdistortion\t9.5000\t65.0000\t0",
        "custom_fx_module\tpitch\t-8.0000\t95.0000\t0",
        "custom_fx_module\treverb\t28.0000\t72.0000\t42.0000",
        "custom_fx_module\tlowpass\t6200.0000\t0\t0",
    ]
    assert "custom_fx_enabled\t1" in text
    assert "custom_fx_wet\t82.00" in text
