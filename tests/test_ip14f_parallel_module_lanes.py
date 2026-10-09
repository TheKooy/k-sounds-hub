from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ksound_hub.module_dev import (
    ModuleWorkContract,
    build_work_contract,
    classify_touched_paths,
    verify_work_contract,
)
from ksound_hub.module_manager import ModuleManager
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, MODULE_API_VERSION, ModuleRegistry

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def _settings(*, installed=DEFAULT_MODULE_IDS, enabled=DEFAULT_MODULE_IDS):
    return SimpleNamespace(installed_modules=list(installed), enabled_modules=list(enabled))


def test_ip14f_is_core_build_while_module_versions_stay_independent():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'Hub IP14' in WEB
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    assert MODULE_API_VERSION == 1
    assert {item.module_id: item.version for item in registry.manifests()} == {
        "eq": "1.0.0",
        "micro_tune": "1.1.0",
        "hard_gate": "1.0.0",
        "voice_fx": "1.2.0",
        "pads": "1.0.0",
        "remote": "1.0.0",
    }


def test_builtin_versions_live_in_per_module_manifests_not_core_registry():
    registry_text = (ROOT / "src/ksound_hub/module_registry.py").read_text(encoding="utf-8")
    assert 'BUILTIN_MANIFEST_ROOT' in registry_text
    assert '"version": "1.0.0"' not in registry_text
    expected_versions = {
        "eq": "1.0.0",
        "micro_tune": "1.1.0",
        "hard_gate": "1.0.0",
        "voice_fx": "1.2.0",
        "pads": "1.0.0",
        "remote": "1.0.0",
    }
    for module_id in DEFAULT_MODULE_IDS:
        path = ROOT / "src/ksound_hub/builtin_modules" / module_id / "module.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == module_id
        assert data["version"] == expected_versions[module_id]
        assert data["ksounds_api"] == MODULE_API_VERSION
        assert path.relative_to(ROOT).as_posix() in data["owned_paths"]


def test_owned_paths_have_one_owner_and_shared_hotspots_are_explicit():
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    owners: dict[str, str] = {}
    for manifest in registry.manifests():
        assert manifest.owned_paths
        for path in manifest.owned_paths:
            assert path not in owners, f"{path} owned by both {owners[path]} and {manifest.module_id}"
            owners[path] = manifest.module_id
    assert owners["src/ksound_hub/builtin_modules/voice_fx/module.json"] == "voice_fx"
    assert owners["src/ksound_hub/builtin_modules/eq/module.json"] == "eq"
    voice = registry.manifest("voice_fx")
    assert "native_engine/src/voice_fx_dsp.cpp" in voice.owned_paths
    assert "native_engine/src/voice_fx_dsp.hpp" in voice.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in voice.shared_paths
    tune = registry.manifest("micro_tune")
    assert "native_engine/src/micro_tune_dsp.cpp" in tune.owned_paths
    assert "native_engine/src/micro_tune_dsp.hpp" in tune.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in tune.shared_paths
    gate = registry.manifest("hard_gate")
    assert "native_engine/src/hard_gate_dsp.cpp" in gate.owned_paths
    assert "native_engine/src/hard_gate_dsp.hpp" in gate.owned_paths
    assert "native_engine/src/micro_engine.cpp" not in gate.shared_paths
    assert "src/ksound_hub/glass_app.py" in registry.manifest("pads").shared_paths
    assert "src/ksound_hub/glass_app.py" in registry.manifest("remote").shared_paths


def test_lane_guard_accepts_owned_files_but_escalates_shared_and_foreign_paths():
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    voice = registry.manifest("voice_fx")
    result = classify_touched_paths(
        voice,
        [
            "src/ksound_hub/voice_fx_ui.py",
            "native_engine/src/voice_fx_dsp.cpp",
            "native_engine/src/micro_engine.cpp",
            "src/ksound_hub/audio/pipewire_v2_final.py",
        ],
    )
    assert result.owned == (
        "src/ksound_hub/voice_fx_ui.py",
        "native_engine/src/voice_fx_dsp.cpp",
    )
    assert result.shared == ()
    assert result.foreign == (
        "native_engine/src/micro_engine.cpp",
        "src/ksound_hub/audio/pipewire_v2_final.py",
    )
    assert result.parallel_safe is False


def test_micro_tune_lane_owns_native_dsp_but_not_core_transport():
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    tune = registry.manifest("micro_tune")
    result = classify_touched_paths(
        tune,
        [
            "src/ksound_hub/micro_processing_ui.py",
            "native_engine/src/micro_tune_dsp.cpp",
            "native_engine/src/micro_engine.cpp",
        ],
    )
    assert result.owned == (
        "src/ksound_hub/micro_processing_ui.py",
        "native_engine/src/micro_tune_dsp.cpp",
    )
    assert result.shared == ()
    assert result.foreign == ("native_engine/src/micro_engine.cpp",)
    assert result.parallel_safe is False


def test_module_contract_ignores_unrelated_module_changes_but_detects_own_changes(tmp_path):
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    voice = registry.manifest("voice_fx")

    repo = tmp_path / "repo"
    for rel in voice.owned_paths + voice.shared_paths:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"baseline {rel}\n", encoding="utf-8")
    glass = repo / "src/ksound_hub/glass_app.py"
    glass.parent.mkdir(parents=True, exist_ok=True)
    glass.write_text('APP_BUILD = "IP14f"\n', encoding="utf-8")

    contract = build_work_contract(repo, voice)
    assert contract.module_id == "voice_fx"
    assert contract.module_version == "1.2.0"
    assert contract.ksounds_api == MODULE_API_VERSION
    assert contract.core_build == "IP14f"

    # A parallel EQ-only file can change without invalidating Voice FX's base.
    unrelated = repo / "src/ksound_hub/eq_only_future.py"
    unrelated.write_text("new eq code\n", encoding="utf-8")
    assert verify_work_contract(repo, voice, contract) == ()

    owned = repo / "src/ksound_hub/voice_fx_ui.py"
    owned.write_text("changed by another Voice FX lane\n", encoding="utf-8")
    errors = verify_work_contract(repo, voice, contract)
    assert errors == ("owned path changed: src/ksound_hub/voice_fx_ui.py",)


def test_shared_hash_check_is_optional_so_disjoint_lanes_can_progress(tmp_path):
    registry = ModuleRegistry(_settings(), external_root=ROOT / ".test-no-modules")
    voice = registry.manifest("voice_fx")
    repo = tmp_path / "repo"
    for rel in voice.owned_paths + voice.shared_paths:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n", encoding="utf-8")
    (repo / "src/ksound_hub/glass_app.py").write_text('APP_BUILD = "IP14f"\n', encoding="utf-8")
    contract = build_work_contract(repo, voice)

    shared = repo / "src/ksound_hub/glass_app.py"
    shared.write_text("APP_BUILD = \"IP14future\"\n", encoding="utf-8")
    assert verify_work_contract(repo, voice, contract, check_shared=False) == ()
    strict_errors = verify_work_contract(repo, voice, contract, check_shared=True)
    assert strict_errors == ("shared path changed: src/ksound_hub/glass_app.py",)


def test_runtime_tracks_loaded_module_version_separately_from_installed_version(tmp_path):
    external = tmp_path / "modules"
    demo_dir = external / "demo_fx"
    demo_dir.mkdir(parents=True)

    def write(version: str):
        (demo_dir / "module.json").write_text(
            json.dumps(
                {
                    "id": "demo_fx",
                    "name": "Demo FX",
                    "version": version,
                    "ksounds_api": MODULE_API_VERSION,
                    "runtimes": ["demo.runtime"],
                }
            ),
            encoding="utf-8",
        )

    write("1.0.0")
    settings = _settings(
        installed=list(DEFAULT_MODULE_IDS) + ["demo_fx"],
        enabled=list(DEFAULT_MODULE_IDS) + ["demo_fx"],
    )
    registry = ModuleRegistry(settings, external_root=external)
    manager = ModuleManager(registry)
    manager.mark_runtime_state("demo_fx", "running")
    assert manager.status("demo_fx").loaded_version == "1.0.0"

    # Simulate a package update while the old runtime is still alive.
    write("1.1.0")
    registry.reload_external_manifests()
    status = manager.status("demo_fx")
    assert status.version == "1.1.0"
    assert status.loaded_version == "1.0.0"
    snap = next(item for item in manager.snapshot()["modules"] if item["id"] == "demo_fx")
    assert snap["version"] == "1.1.0"
    assert snap["loaded_version"] == "1.0.0"

    manager.mark_runtime_state("demo_fx", "stopped")
    assert manager.status("demo_fx").loaded_version == ""


def test_contract_round_trip_is_stable():
    original = ModuleWorkContract(
        schema=1,
        module_id="voice_fx",
        module_version="1.0.0",
        ksounds_api=1,
        core_build="IP14f",
        owned_hashes={"a": "1" * 64},
        shared_hashes={"b": None},
    )
    restored = ModuleWorkContract.from_dict(original.to_dict())
    assert restored == original
