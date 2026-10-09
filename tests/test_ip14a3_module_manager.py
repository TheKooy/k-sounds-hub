from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksound_hub.module_manager import ModuleManager, ModulePhase
from ksound_hub.module_packages import LocalModulePackageInstaller, parse_catalog
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry
from ksound_hub.soundboard_remote_runtime import RemoteRuntimeConfig

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")
RUNTIME = (ROOT / "src/ksound_hub/soundboard_remote_runtime.py").read_text(encoding="utf-8")


def _settings(enabled=DEFAULT_MODULE_IDS):
    return SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(enabled),
    )


def test_ip14a3_build_marker_and_manager_are_wired():
    assert 'APP_BUILD = "IP' in GLASS
    assert "self.module_manager = ModuleManager(self.module_registry)" in GLASS
    assert "self.module_package_installer = LocalModulePackageInstaller(self.module_manager)" in GLASS
    assert "snapshot = self.module_manager.snapshot()" in GLASS
    assert "Installed · {source}" in GLASS
    assert "RuntimeSupervisor" in GLASS


def test_existing_ip13_user_migrates_with_all_bundled_modules_installed_enabled():
    settings = _settings()
    registry = ModuleRegistry(settings)
    assert tuple(settings.installed_modules) == tuple(DEFAULT_MODULE_IDS)
    assert tuple(settings.enabled_modules) == tuple(DEFAULT_MODULE_IDS)
    for module_id in DEFAULT_MODULE_IDS:
        assert registry.is_installed(module_id)
        assert registry.is_enabled(module_id)
        assert registry.manifest(module_id).builtin is True
        assert registry.manifest(module_id).removable is False


def test_manager_keeps_install_enable_runtime_as_separate_states():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)

    remote = manager.status("remote")
    assert remote.installed is True
    assert remote.enabled is True
    assert remote.runtime_required is True
    assert remote.phase == ModulePhase.ENABLED
    assert remote.runtime_state == "unconfirmed"

    manager.mark_runtime_state("remote", "running")
    assert manager.status("remote").phase == ModulePhase.RUNNING

    ok, _ = manager.request_enable("pads", False)
    assert ok
    ok, _ = manager.request_enable("voice_fx", False)
    assert ok
    idle = manager.status("remote")
    assert idle.installed is True
    assert idle.enabled is True
    assert idle.runtime_required is False
    assert idle.runtime_state == "idle"
    assert idle.phase == ModulePhase.IDLE

    ok, _ = manager.request_enable("remote", False)
    assert ok
    disabled = manager.status("remote")
    assert disabled.installed is True
    assert disabled.enabled is False
    assert disabled.phase == ModulePhase.INSTALLED_DISABLED


def test_remote_is_single_shared_conditional_runtime():
    registry = ModuleRegistry(_settings())
    assert registry.remote_surfaces() == ("pads", "voice_fx")
    registry.set_enabled("pads", False)
    assert registry.remote_surfaces() == ("voice_fx",)
    registry.set_enabled("voice_fx", False)
    assert registry.remote_surfaces() == ()
    assert registry.remote_runtime_required() is False
    registry.set_enabled("pads", True)
    assert registry.remote_surfaces() == ("pads",)
    registry.set_enabled("remote", False)
    assert registry.remote_surfaces() == ()
    assert registry.remote_runtime_required() is False

    config = RemoteRuntimeConfig.from_environment(ROOT).with_surfaces([])
    assert config.surfaces == ()
    assert "if not runtime_config.surfaces:" in RUNTIME
    assert "Remote server idle: no enabled Remote surfaces" in RUNTIME


def test_remote_web_remains_surface_adaptive_and_has_ip14_family_marker():
    assert "KSH_REMOTE_SURFACES" in WEB
    assert 'remote_surface_enabled("pads")' in WEB
    assert 'remote_surface_enabled("voice_fx")' in WEB
    assert "Remote IP10t · Hub IP14" in WEB


def test_catalog_parser_is_ready_for_git_backed_index_without_network_side_effects():
    payload = {
        "schema": 1,
        "modules": {
            "spectrum": {
                "name": "Spectrum",
                "version": "1.2.3",
                "download": "https://example.invalid/spectrum-1.2.3.ksmodule",
                "sha256": "a" * 64,
                "ksounds_api": 1,
            }
        },
    }
    entries = parse_catalog(payload)
    assert len(entries) == 1
    assert entries[0].module_id == "spectrum"
    assert entries[0].download_url.endswith(".ksmodule")


def _write_module_archive(path: Path, module_id: str = "demo_meter") -> str:
    manifest = {
        "id": module_id,
        "name": "Demo Meter",
        "version": "1.0.0",
        "ksounds_api": 1,
        "provides": ["ui.demo_meter"],
        "nav_label": "Meter",
        "nav_icon": "M",
        "removable": True,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("module.json", json.dumps(manifest))
        package.writestr("README.txt", "payload only; no code is executed by the package installer\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_local_ksmodule_install_is_atomic_disabled_by_default_and_uninstall_requires_disabled(tmp_path):
    external_root = tmp_path / "modules"
    settings = _settings()
    registry = ModuleRegistry(settings, external_root=external_root)
    manager = ModuleManager(registry)
    installer = LocalModulePackageInstaller(manager)

    archive = tmp_path / "demo.ksmodule"
    digest = _write_module_archive(archive)
    installed = installer.install_archive(archive, expected_sha256=digest)
    assert installed.module_id == "demo_meter"
    assert registry.is_installed("demo_meter") is True
    assert registry.is_enabled("demo_meter") is False
    assert (external_root / "demo_meter" / "module.json").is_file()
    assert manager.status("demo_meter").phase == ModulePhase.INSTALLED_DISABLED
    assert manager.can_uninstall("demo_meter") is True

    ok, _ = manager.request_enable("demo_meter", True)
    assert ok
    assert manager.can_uninstall("demo_meter") is False
    assert installer.uninstall("demo_meter") is False

    ok, _ = manager.request_enable("demo_meter", False)
    assert ok
    assert installer.uninstall("demo_meter") is True
    assert not (external_root / "demo_meter").exists()
    assert registry.manifest("demo_meter") is None


def test_ksmodule_rejects_path_traversal(tmp_path):
    external_root = tmp_path / "modules"
    registry = ModuleRegistry(_settings(), external_root=external_root)
    installer = LocalModulePackageInstaller(ModuleManager(registry))
    archive = tmp_path / "bad.ksmodule"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("module.json", json.dumps({"id": "bad_module", "name": "Bad", "version": "1"}))
        package.writestr("../escape.txt", "nope")
    with pytest.raises(ValueError, match="unsafe archive path"):
        installer.inspect_archive(archive)


def test_bundled_modules_cannot_be_uninstalled_in_ip14a3():
    registry = ModuleRegistry(_settings(enabled=()))
    manager = ModuleManager(registry)
    for module_id in DEFAULT_MODULE_IDS:
        assert manager.can_uninstall(module_id) is False
        assert registry.set_installed(module_id, False) is False


def test_historical_ip13_contracts_do_not_pin_current_major_build():
    stale = []
    for path in sorted((ROOT / "tests").glob("test_ip13*.py")):
        text = path.read_text(encoding="utf-8")
        if 'APP_BUILD = "IP13' in text or 'Remote IP10t · Hub IP13' in text:
            stale.append(path.name)
    assert stale == []
