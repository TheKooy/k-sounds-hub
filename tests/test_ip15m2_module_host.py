"""A self-contained module probe; no real audio, MIDI or user modules touched."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksound_hub.module_host import ModuleProcess, runner_path, PROTOCOL
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry
from ksound_hub.module_manager import ModuleManager
from ksound_hub.module_packages import LocalModulePackageInstaller

RUNNER = '''import json, sys
print(json.dumps({"type": "hello", "protocol": "ksounds-jsonl/1", "id": "module_probe", "version": "0.1.0"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"seq": message["seq"], "ok": True, "result": {"echo": message["command"]}}), flush=True)
'''


def _archive(tmp_path: Path, entrypoint="runtime/main.py"):
    package = tmp_path / "module_probe.ksmodule"
    manifest = {
        "id": "module_probe",
        "name": "Module Probe",
        "version": "0.1.0",
        "ksounds_api": 1,
        "runner": {"protocol": PROTOCOL, "entrypoint": entrypoint},
    }
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("module.json", json.dumps(manifest))
        zf.writestr("runtime/main.py", RUNNER)
    return package


def _install(tmp_path):
    settings = SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(DEFAULT_MODULE_IDS),
    )
    registry = ModuleRegistry(settings, external_root=tmp_path / "modules")
    installer = LocalModulePackageInstaller(ModuleManager(registry))
    installer.install_archive(_archive(tmp_path))
    assert not registry.is_enabled("module_probe")
    return registry, installer


def test_external_install_hello_request_uninstall(tmp_path):
    registry, installer = _install(tmp_path)
    with ModuleProcess(registry.external_root / "module_probe") as process:
        assert process.running
        reply = process.request("status")
        assert reply == {"seq": 1, "ok": True, "result": {"echo": "status"}}
    assert not process.running
    assert installer.uninstall("module_probe")
    assert not registry.is_installed("module_probe")


@pytest.mark.parametrize("entrypoint", ["../outside.py", "/tmp/outside.py", "runtime\\main.py", "runtime//main.py", "module.json"])
def test_unsafe_entrypoint_rejected(tmp_path, entrypoint):
    _install(tmp_path)
    root = tmp_path / "modules/module_probe"
    config = json.loads((root / "module.json").read_text())
    config["runner"]["entrypoint"] = entrypoint
    (root / "module.json").write_text(json.dumps(config))
    with pytest.raises((ValueError, FileNotFoundError)):
        runner_path(root)


def test_symlink_entrypoint_rejected(tmp_path):
    _install(tmp_path)
    root = tmp_path / "modules/module_probe"
    (root / "runtime/main.py").unlink()
    (root / "runtime/main.py").symlink_to(tmp_path / "outside.py")
    (tmp_path / "outside.py").write_text(RUNNER)
    with pytest.raises(ValueError):
        runner_path(root)
