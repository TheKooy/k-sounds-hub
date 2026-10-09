from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksound_hub.module_manager import ModuleCatalogEntry, ModuleManager, ModulePhase
from ksound_hub.module_packages import LocalModulePackageInstaller
from ksound_hub.module_registry import (
    DEFAULT_MODULE_IDS,
    MODULE_API_VERSION,
    ModuleManifest,
    ModuleRegistry,
    module_version_at_least,
    module_version_tuple,
    normalize_module_version,
)

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def _settings(*, installed=DEFAULT_MODULE_IDS, enabled=DEFAULT_MODULE_IDS):
    return SimpleNamespace(
        installed_modules=list(installed),
        enabled_modules=list(enabled),
    )


def _write_module(path: Path, module_id: str, version: str, *, depends=None, min_versions=None) -> str:
    manifest = {
        "id": module_id,
        "name": module_id.replace("_", " ").title(),
        "version": version,
        "ksounds_api": MODULE_API_VERSION,
        "depends": list(depends or []),
        "min_versions": dict(min_versions or {}),
        "removable": True,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("module.json", json.dumps(manifest))
        package.writestr("payload.txt", f"{module_id} {version}\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ip14e2_core_marker_and_module_ui_expose_independent_versions():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'Hub IP14' in WEB
    assert 'version_text = f"v{status.version}"' in GLASS
    assert 'v{status.catalog_version} available' in GLASS


def test_builtin_modules_start_on_semver_without_changing_core_api():
    registry = ModuleRegistry(_settings())
    assert MODULE_API_VERSION == 1
    versions = {manifest.module_id: manifest.version for manifest in registry.manifests() if manifest.builtin}
    assert versions == {
        "eq": "1.0.0",
        "micro_tune": "1.1.0",
        "hard_gate": "1.0.0",
        "voice_fx": "1.2.0",
        "pads": "1.0.0",
        "remote": "1.0.0",
    }
    for version in versions.values():
        assert len(module_version_tuple(version)) == 3


def test_legacy_module_versions_normalize_to_semver():
    assert normalize_module_version("1") == "1.0.0"
    assert normalize_module_version("2.4") == "2.4.0"
    assert normalize_module_version("2.4.7") == "2.4.7"
    assert module_version_at_least("2.4", "2.3.9") is True
    with pytest.raises(ValueError, match="invalid module version"):
        normalize_module_version("latest")


def test_catalog_can_advance_one_module_without_bumping_the_core():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    manager.set_catalog(
        [
            ModuleCatalogEntry.from_dict(
                {
                    "id": "voice_fx",
                    "name": "Voice FX",
                    "version": "1.3.0",
                    "download": "https://example.invalid/voice_fx-1.3.0.ksmodule",
                    "sha256": "a" * 64,
                    "ksounds_api": MODULE_API_VERSION,
                }
            )
        ]
    )
    status = manager.status("voice_fx")
    assert status.version == "1.2.0"
    assert status.catalog_version == "1.3.0"
    assert status.update_available is True
    assert status.core_api == MODULE_API_VERSION
    snap = manager.snapshot()["modules"]
    voice = next(item for item in snap if item["id"] == "voice_fx")
    assert voice["version"] == "1.2.0"
    assert voice["catalog_version"] == "1.3.0"
    assert voice["update_available"] is True


def test_dependency_min_versions_are_checked_separately_from_presence(tmp_path):
    external = tmp_path / "modules"
    child = external / "demo_consumer"
    child.mkdir(parents=True)
    (child / "module.json").write_text(
        json.dumps(
            {
                "id": "demo_consumer",
                "name": "Demo Consumer",
                "version": "1.0.0",
                "ksounds_api": MODULE_API_VERSION,
                "depends": ["eq"],
                "min_versions": {"eq": "1.1.0"},
            }
        ),
        encoding="utf-8",
    )
    installed = list(DEFAULT_MODULE_IDS) + ["demo_consumer"]
    registry = ModuleRegistry(_settings(installed=installed, enabled=DEFAULT_MODULE_IDS), external_root=external)
    assert registry.missing_dependencies("demo_consumer") == ()
    assert registry.incompatible_dependencies("demo_consumer") == ("eq>=1.1.0",)
    manager = ModuleManager(registry)
    ok, message = manager.request_enable("demo_consumer", True)
    assert ok is False
    assert "Dependency version mismatch" in message


def test_external_module_update_is_atomic_versioned_and_keeps_module_disabled(tmp_path):
    external = tmp_path / "modules"
    registry = ModuleRegistry(_settings(), external_root=external)
    manager = ModuleManager(registry)
    installer = LocalModulePackageInstaller(manager)

    v1 = tmp_path / "demo-1.0.0.ksmodule"
    v2 = tmp_path / "demo-1.1.0.ksmodule"
    old = tmp_path / "demo-0.9.0.ksmodule"
    digest1 = _write_module(v1, "demo_meter", "1.0.0")
    digest2 = _write_module(v2, "demo_meter", "1.1.0")
    _write_module(old, "demo_meter", "0.9.0")

    installed = installer.install_archive(v1, expected_sha256=digest1)
    assert installed.version == "1.0.0"
    assert registry.is_enabled("demo_meter") is False

    updated = installer.update_archive(v2, expected_sha256=digest2)
    assert updated.version == "1.1.0"
    assert registry.manifest("demo_meter").version == "1.1.0"
    assert registry.is_installed("demo_meter") is True
    assert registry.is_enabled("demo_meter") is False
    assert manager.status("demo_meter").phase == ModulePhase.INSTALLED_DISABLED
    assert not (external / ".demo_meter.previous").exists()

    with pytest.raises(ValueError, match="must be newer"):
        installer.update_archive(old)
    assert registry.manifest("demo_meter").version == "1.1.0"


def test_manifest_snapshot_keeps_core_api_and_per_module_version_constraints(tmp_path):
    external = tmp_path / "modules"
    child = external / "demo_fx"
    child.mkdir(parents=True)
    (child / "module.json").write_text(
        json.dumps(
            {
                "id": "demo_fx",
                "name": "Demo FX",
                "version": "3.2",
                "ksounds_api": MODULE_API_VERSION,
                "depends": ["eq"],
                "min_versions": {"eq": "1"},
            }
        ),
        encoding="utf-8",
    )
    registry = ModuleRegistry(
        _settings(installed=list(DEFAULT_MODULE_IDS) + ["demo_fx"], enabled=DEFAULT_MODULE_IDS),
        external_root=external,
    )
    manifest: ModuleManifest = registry.manifest("demo_fx")
    assert manifest.version == "3.2.0"
    assert dict(manifest.min_versions) == {"eq": "1.0.0"}
    snap = registry.snapshot()
    demo = next(item for item in snap["modules"] if item["id"] == "demo_fx")
    assert demo["version"] == "3.2.0"
    assert demo["ksounds_api"] == MODULE_API_VERSION
    assert demo["min_versions"] == {"eq": "1.0.0"}
