from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ksound_hub.module_dev import classify_touched_paths
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")
MICRO = (ROOT / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")
VOICE_CPP = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")
VOICE_HPP = (ROOT / "native_engine/src/voice_fx_dsp.hpp").read_text(encoding="utf-8")
CMAKE = (ROOT / "native_engine/CMakeLists.txt").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "src/ksound_hub/builtin_modules/voice_fx/module.json").read_text(encoding="utf-8"))


def _settings():
    return SimpleNamespace(installed_modules=list(DEFAULT_MODULE_IDS), enabled_modules=list(DEFAULT_MODULE_IDS))


def test_ip14g5_is_core_refactor_without_fake_voice_fx_version_bump():
    # Historical refactor contract: later IP14 Core revisions must not make
    # this Voice FX ownership test stale just by changing the Core build tag.
    assert 'APP_BUILD = "IP' in GLASS
    assert "Hub IP14" in WEB
    assert MANIFEST["version"] == "1.2.0"
    assert MANIFEST["ksounds_api"] == 1


def test_native_voice_and_custom_fx_are_physically_split_from_micro_engine():
    assert '#include "voice_fx_dsp.hpp"' in MICRO
    assert "ksound::voice_fx::Processor micro_voice_fx;" in MICRO
    assert "ksound::voice_fx::Processor micro_custom_fx;" in MICRO
    assert "micro_voice_fx.process(" in MICRO
    assert "micro_custom_fx.process_custom(" in MICRO
    assert "struct VoiceFxDsp" not in MICRO
    assert "struct CustomFxModuleDsp" not in MICRO
    assert "void apply_single_voice_fx(" not in MICRO
    assert "void apply_custom_fx(" not in MICRO
    assert "struct VoiceFxDsp" in VOICE_CPP
    assert "struct CustomFxModuleDsp" in VOICE_CPP
    assert "void Processor::process(" in VOICE_CPP
    assert "void Processor::process_custom(" in VOICE_CPP
    assert "struct CustomModuleSpec" in VOICE_HPP
    assert "class Processor" in VOICE_HPP
    assert "struct Impl;" in VOICE_HPP


def test_cmake_builds_voice_fx_as_a_separate_translation_unit():
    micro_target = CMAKE.split("add_executable(ksound_native_micro_engine", 1)[1].split(")", 1)[0]
    assert "src/micro_engine.cpp" in micro_target
    assert "src/voice_fx_dsp.cpp" in micro_target


def test_voice_fx_lane_owns_native_dsp_but_not_core_micro_engine():
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    voice = registry.manifest("voice_fx")
    assert voice is not None
    assert "native_engine/src/voice_fx_dsp.cpp" in voice.owned_paths
    assert "native_engine/src/voice_fx_dsp.hpp" in voice.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in voice.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in voice.shared_paths
    classified = classify_touched_paths(
        voice,
        ["native_engine/src/voice_fx_dsp.cpp", "native_engine/src/micro_engine.cpp"],
    )
    assert classified.owned == ("native_engine/src/voice_fx_dsp.cpp",)
    assert classified.shared == ()
    assert classified.foreign == ("native_engine/src/micro_engine.cpp",)


def test_historical_and_custom_fx_algorithms_live_in_module_owned_source():
    for effect in ("radio", "telephone", "robot", "church", "church_pa"):
        assert f'"{effect}"' in VOICE_CPP
    for custom_type in ("gain", "highpass", "lowpass", "saturation", "delay"):
        assert f'"{custom_type}"' in VOICE_CPP


def test_historical_contracts_follow_module_owned_native_source():
    preset_contract = (ROOT / "tests/test_ip14c3_preset_engine.py").read_text(encoding="utf-8")
    voice_contract = (ROOT / "tests/test_voice_fx_ip13a.py").read_text(encoding="utf-8")
    assert 'native_engine/src/voice_fx_dsp.cpp' in preset_contract
    assert "'struct CustomFxModuleDsp' in VOICE_FX_DSP" in preset_contract
    assert 'micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);' in preset_contract
    assert 'native_engine/src/voice_fx_dsp.cpp' in voice_contract
    assert "'void apply_voice_fx(' in dsp" in voice_contract
    assert 'micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);' in voice_contract

    for rel in (
        "tests/test_ip13b3_contract.py",
        "tests/test_ip13c_contract.py",
        "tests/test_ip13g_contract.py",
        "tests/test_ip13g2_contract.py",
        "tests/test_ip13h_contract.py",
        "tests/test_ip13h2_contract.py",
    ):
        contract = (ROOT / rel).read_text(encoding="utf-8")
        assert 'native_engine/src/voice_fx_dsp.cpp' in contract

    ip13g = (ROOT / "tests/test_ip13g_contract.py").read_text(encoding="utf-8")
    assert 'CORE_NATIVE = (ROOT / "native_engine/src/micro_engine.cpp")' in ip13g
    assert "'const std::array<std::string, 5> presets' in CORE_NATIVE" in ip13g
    assert "'std::string(\"church\"), std::string(\"church_pa\")' in CORE_NATIVE" in ip13g


def test_native_realtime_contract_links_module_translation_units():
    runtime_contract = (ROOT / "tests/test_native_realtime_backlog.py").read_text(encoding="utf-8")
    assert 'VOICE_FX_DSP = ROOT / "native_engine" / "src" / "voice_fx_dsp.cpp"' in runtime_contract
    assert 'MICRO_TUNE_DSP = ROOT / "native_engine" / "src" / "micro_tune_dsp.cpp"' in runtime_contract
    compile_line = '[compiler, "-std=c++20", "-O2", str(MICRO_ENGINE), str(VOICE_FX_DSP), str(MICRO_TUNE_DSP), "-ldl", "-o", str(binary)]'
    assert runtime_contract.count(compile_line) == 3
