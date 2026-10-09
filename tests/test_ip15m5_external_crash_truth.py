"""IP15m5: runtime failure must not rewrite installed/enabled user intent."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ksound_hub.module_extensions import ExtensionRegistry
from ksound_hub.module_host import ExternalModuleHost, PROTOCOL
from ksound_hub.module_manager import ModuleManager, ModulePhase
from ksound_hub.module_packages import LocalModulePackageInstaller
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry


RUNNER = """import json, sys
print(json.dumps({"type":"hello","protocol":"ksounds-jsonl/1","id":"lifecycle_probe","version":"1.0.0"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"seq":message["seq"],"ok":True,"result":{}}), flush=True)
"""


def rig(tmp_path: Path):
    settings = SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(DEFAULT_MODULE_IDS),
    )
    registry = ModuleRegistry(settings, external_root=tmp_path / "modules")
    manager = ModuleManager(registry)
    installer = LocalModulePackageInstaller(manager)
    package = tmp_path / "lifecycle_probe.ksmodule"
    manifest = {
        "id": "lifecycle_probe",
        "name": "Lifecycle Probe",
        "version": "1.0.0",
        "ksounds_api": 1,
        "runner": {"protocol": PROTOCOL, "entrypoint": "runtime.py"},
        "extensions": {
            "schema": 1,
            "desktop": [{"id": "home", "label": "Lifecycle Probe", "controls": []}],
            "remote": [{"id": "remote", "label": "Lifecycle Probe", "controls": []}],
        },
    }
    with zipfile.ZipFile(package, "w") as zf:
        zf.writestr("module.json", json.dumps(manifest))
        zf.writestr("runtime.py", RUNNER)
    installer.install_archive(package)
    host = ExternalModuleHost(manager)
    extensions = ExtensionRegistry(registry, host)
    return registry, manager, installer, host, extensions


def test_runtime_failure_keeps_enabled_choice_and_clears_loaded_runtime(tmp_path):
    registry, manager, _installer, host, extensions = rig(tmp_path)

    assert manager.request_enable("lifecycle_probe", True)[0]
    host.start("lifecycle_probe")
    assert registry.is_enabled("lifecycle_probe")
    assert manager.status("lifecycle_probe").phase == ModulePhase.RUNNING
    assert manager.status("lifecycle_probe").loaded_version == "1.0.0"
    assert [s.surface_id for s in extensions.active_surfaces("desktop")] == ["home"]
    assert [s.surface_id for s in extensions.active_surfaces("remote")] == ["remote"]

    process = host._processes["lifecycle_probe"].process
    assert process is not None
    process.kill()
    process.wait(timeout=2)

    assert host.crashed() == ("lifecycle_probe",)
    manager.mark_runtime_failure("lifecycle_probe", "worker exited")

    status = manager.status("lifecycle_probe")
    assert registry.is_enabled("lifecycle_probe") is True
    assert status.enabled is True
    assert status.phase == ModulePhase.ERROR
    assert status.runtime_state == "error"
    assert status.loaded_version == ""
    assert status.detail == "worker exited"
    assert extensions.active_surfaces("desktop") == ()
    assert extensions.active_surfaces("remote") == ()

    host.start("lifecycle_probe")
    status = manager.status("lifecycle_probe")
    assert status.enabled is True
    assert status.phase == ModulePhase.RUNNING
    assert status.runtime_state == "running"
    assert [s.surface_id for s in extensions.active_surfaces("desktop")] == ["home"]

    host.stop("lifecycle_probe")


def test_glass_crash_handler_never_disables_user_preference():
    source = Path(__file__).resolve().parents[1] / "src/ksound_hub/glass_app.py"
    text = source.read_text(encoding="utf-8")
    start = text.index("    def _check_external_module_processes(self) -> None:")
    end = text.index("\n    def module_enabled", start)
    body = text[start:end]

    assert "set_enabled(mid, False)" not in body
    assert "mark_runtime_failure" in body
    assert "self._module_values.pop(mid, None)" in body
    assert "module_ui_state_changed.emit(mid)" in body
    assert "module_state_changed.emit(mid)" in body
