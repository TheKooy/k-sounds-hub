from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ksound_hub.models import AppSettings
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry

ROOT = Path(__file__).resolve().parents[1]
MICRO = (ROOT / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")
GATE = (ROOT / "native_engine/src/hard_gate_dsp.hpp").read_text(encoding="utf-8")
CMAKE = (ROOT / "native_engine/CMakeLists.txt").read_text(encoding="utf-8")
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def _settings(*, installed=DEFAULT_MODULE_IDS, enabled=DEFAULT_MODULE_IDS):
    return SimpleNamespace(installed_modules=list(installed), enabled_modules=list(enabled))


def test_hard_gate_is_a_real_builtin_module_with_independent_native_lane():
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    manifest = registry.manifest("hard_gate")
    assert manifest is not None
    assert manifest.version == "1.0.0"
    assert manifest.ksounds_api == 1
    assert manifest.runtimes == ("audio.micro",)
    assert "native_engine/src/hard_gate_dsp.cpp" in manifest.owned_paths
    assert "native_engine/src/hard_gate_dsp.hpp" in manifest.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in manifest.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in manifest.shared_paths


def test_hard_gate_algorithm_is_out_of_core_and_builds_as_module_source():
    assert '#include "hard_gate_dsp.hpp"' in MICRO
    assert "src/hard_gate_dsp.cpp" in CMAKE
    assert "class Processor" in GATE
    assert "threshold_to_peak" in GATE
    assert "hold_chunks_ = 18" in GATE
    assert "target_gain > start_gain ? 96 : 288" in GATE
    for marker in (
        "struct HardGateState",
        "hard_gate_threshold_to_peak(",
        "chunk_peak_for_gate(",
        "apply_hard_gate_to_micro_chunk(",
    ):
        assert marker not in MICRO


def test_pipeline_order_is_still_tune_pre_gate_then_gate_then_tune_post_then_vfx():
    pre = MICRO.index("micro_tune.process_pre_gate(mic_chunk, tune);")
    gate = MICRO.index("micro_gate.process(mic_chunk, hard_gate_settings(state));", pre)
    post = MICRO.index("micro_tune.process_post_gate(mic_chunk, tune);", gate)
    voice = MICRO.index(
        "micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);",
        post,
    )
    custom = MICRO.index(
        "micro_custom_fx.process_custom(mic_chunk, state.custom_fx_enabled, state.custom_fx_wet, state.custom_fx_modules);",
        voice,
    )
    assert pre < gate < post < voice < custom


def test_pre_ip14k_settings_migrate_hard_gate_module_once_without_reenabling_future_disable():
    legacy = AppSettings.from_dict({
        "installed_modules": ["eq", "micro_tune", "voice_fx", "pads", "remote"],
        "enabled_modules": ["eq", "micro_tune", "voice_fx", "pads", "remote"],
    })
    assert "hard_gate" in legacy.installed_modules
    assert "hard_gate" in legacy.enabled_modules

    after_user_disable = AppSettings.from_dict({
        "installed_modules": ["eq", "micro_tune", "hard_gate", "voice_fx", "pads", "remote"],
        "enabled_modules": ["eq", "micro_tune", "voice_fx", "pads", "remote"],
    })
    assert "hard_gate" in after_user_disable.installed_modules
    assert "hard_gate" not in after_user_disable.enabled_modules


def test_registry_migrates_old_five_module_settings_once():
    settings = SimpleNamespace(
        installed_modules=["eq", "micro_tune", "voice_fx", "pads", "remote"],
        enabled_modules=["eq", "micro_tune", "voice_fx", "pads", "remote"],
    )
    registry = ModuleRegistry(settings, external_root=ROOT / ".test-no-modules")
    assert registry.is_installed("hard_gate")
    assert registry.is_enabled("hard_gate")

    settings.enabled_modules.remove("hard_gate")
    registry.reload_external_manifests()
    assert not registry.is_enabled("hard_gate")


def test_runtime_policy_disables_gate_and_tune_independently():
    assert 'hard_gate_enabled = self.module_enabled("hard_gate")' in GLASS
    assert "if not hard_gate_enabled:" in GLASS
    assert "channel.hard_gate_enabled = False" in GLASS
    assert "if not tune_enabled:" in GLASS
    assert "channel.micro_processing_enabled = False" in GLASS


def test_hard_gate_probe_stays_core_owned_as_a_behavior_contract():
    assert 'arg == "--probe-hard-gate"' in MICRO
    assert "hard_gate_sum=" in MICRO
    assert "hard_gate_weighted=" in MICRO
    assert "hard_gate=\" << (ok ? \"ok\" : \"failed\")" in MICRO
