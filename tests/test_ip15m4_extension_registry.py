"""IP15m4a generic graft contract; no audio or real user modules."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksound_hub.module_extensions import ExtensionRegistry, validate_extensions
from ksound_hub.module_host import ExternalModuleHost
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry
from ksound_hub.module_manager import ModuleManager
from ksound_hub.module_packages import LocalModulePackageInstaller

MANIFEST = {
    "id": "example_widget", "name": "Example Widget", "version": "0.1.0", "ksounds_api": 1,
    "runner": {"protocol": "ksounds-jsonl/1", "entrypoint": "runtime/main.py"},
    "extensions": {
        "schema": 1,
        "desktop": [{"id": "home", "label": "Example Widget", "icon": "◇", "controls": [
            {"id": "info", "type": "label", "label": "Example module, not part of K-Sounds"},
            {"id": "hit", "type": "button", "label": "Test", "action": "example.hit"},
            {"id": "gain", "type": "slider", "label": "Gain", "min": 0, "max": 10, "default": 5, "action": "example.gain"},
        ]}],
        "remote": [{"id": "remote", "label": "Remote example", "controls": [
            {"id": "toggle", "type": "toggle", "label": "Example toggle", "action": "example.toggle"},
        ]}],
        "settings": [{"id": "prefs", "label": "Example preferences", "controls": []}],
    }
}
RUNNER = '''import json,sys
print(json.dumps({"type":"hello","protocol":"ksounds-jsonl/1","id":"example_widget","version":"0.1.0"}),flush=True)
for line in sys.stdin:
 data=json.loads(line)
 print(json.dumps({"seq":data["seq"],"ok":True,"result":{"command":data["command"],"payload":data["payload"]}}),flush=True)
'''


def rig(tmp_path, manifest=MANIFEST):
    settings = SimpleNamespace(installed_modules=list(DEFAULT_MODULE_IDS), enabled_modules=list(DEFAULT_MODULE_IDS))
    registry = ModuleRegistry(settings, external_root=tmp_path / "modules")
    manager = ModuleManager(registry)
    installer = LocalModulePackageInstaller(manager)
    host = ExternalModuleHost(manager)
    extensions = ExtensionRegistry(registry, host)
    path = tmp_path / "example.ksmodule"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("module.json", json.dumps(manifest))
        zf.writestr("runtime/main.py", RUNNER)
    return registry, manager, installer, host, extensions, path


def test_no_modules_means_no_grafts(tmp_path):
    _, _, _, _, ex, _ = rig(tmp_path)
    assert ex.active_surfaces("desktop") == ()
    assert ex.active_surfaces("remote") == ()
    assert ex.active_surfaces("settings") == ()


def test_installed_not_enabled_means_no_grafts(tmp_path):
    registry, _, installer, host, ex, archive = rig(tmp_path)
    installer.install_archive(archive)
    assert registry.is_installed("example_widget")
    assert ex.active_surfaces("desktop") == ()
    assert ex.active_surfaces("remote") == ()
    assert not ex.action_allowed("example_widget", "home", "hit", "example.hit")
    host.stop_all()


def test_only_running_worker_contributes_then_disappears(tmp_path):
    registry, manager, installer, host, ex, archive = rig(tmp_path)
    installer.install_archive(archive)
    manager.request_enable("example_widget", True)
    assert ex.active_surfaces("desktop") == ()
    host.start("example_widget")
    surfaces = ex.active_surfaces("desktop")
    assert [s.route for s in surfaces] == ["ext:example_widget:home"]
    assert ex.active_surfaces("remote")[0].surface_id == "remote"
    assert ex.active_surfaces("settings")[0].surface_id == "prefs"
    assert ex.action_allowed("example_widget", "home", "hit", "example.hit")
    assert not ex.action_allowed("example_widget", "home", "hit", "example.different")
    assert not ex.action_allowed("example_widget", "home", "info", "")
    reply = host.request("example_widget", "ui.action", {"surface": "home", "control": "hit", "action": "example.hit", "value": True})
    assert reply["result"]["payload"]["action"] == "example.hit"
    registry.set_enabled("remote", False)
    assert ex.active_surfaces("remote") == ()
    host.stop("example_widget")
    assert ex.active_surfaces("desktop") == ()
    assert ex.active_surfaces("settings") == ()


@pytest.mark.parametrize("mutation", [
    lambda d: d["extensions"].update(schema=2),
    lambda d: d["extensions"]["desktop"].append(d["extensions"]["desktop"][0]),
    lambda d: d["extensions"]["desktop"][0]["controls"][1].update(action=""),
    lambda d: d["extensions"]["desktop"][0]["controls"][2].update(min=100),
    lambda d: d["extensions"]["desktop"][0]["controls"][1].update(type="arbitrary-python"),
])
def test_malformed_extension_rejected_before_install(tmp_path, mutation):
    manifest = json.loads(json.dumps(MANIFEST))
    mutation(manifest)
    registry, _, installer, _, _, archive = rig(tmp_path, manifest)
    with pytest.raises(ValueError):
        installer.install_archive(archive)
    assert not registry.is_installed("example_widget")
    assert not (registry.external_root / "example_widget").exists()


def test_no_extension_legacy_manifest_is_compatible():
    manifest = json.loads(json.dumps(MANIFEST))
    manifest.pop("extensions")
    assert validate_extensions(manifest) == ()


def test_reject_unknown_kind_request(tmp_path):
    *_, ex, _ = rig(tmp_path)
    with pytest.raises(ValueError):
        ex.active_surfaces("hacked")


def test_non_tech_external_module_no_import_of_qt():
    import sys
    assert "PySide6" not in __import__("ksound_hub.module_extensions", fromlist=["__file__"]).__dict__


def test_nav_before_drawer_and_after_runtime_registration(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    from ksound_hub.glass_app import PreviewWindow, Drawer
    app = QApplication.instance() or QApplication([])
    registry, manager, installer, host, extensions, archive = rig(tmp_path)
    installer.install_archive(archive)
    manager.request_enable("example_widget", True)
    host.start("example_widget")
    controller = SimpleNamespace(module_registry=registry, extension_registry=extensions,
                                 invoke_module_ui_action=lambda *args: True)
    stub = SimpleNamespace(backend_controller=controller)
    assert all(r != "ext:example_widget:home" for r, *_ in PreviewWindow._navigation_specs(stub))
    # Render the plugin page without launching the real K-Sounds audio/meter stack.
    dummy = SimpleNamespace(
        _backend_controller=controller,
        _make_scroll_page=lambda page: page,
        _external_action_status_labels={},
    )
    dummy._external_control_widgets = {}
    dummy._register_external_control = lambda surface, control, widget: (
        dummy._external_control_widgets.__setitem__(
            (surface.module_id, surface.surface_id, control.control_id), widget
        )
    )
    dummy._external_surface_widget = lambda surface: Drawer._external_surface_widget(dummy, surface)
    widget = Drawer._external_page(dummy, extensions.active_surfaces("desktop")[0])
    assert isinstance(widget, QWidget)
    stub.drawer = SimpleNamespace(_page_indices={"ext:example_widget:home": 5})
    assert "ext:example_widget:home" in [r for r, *_ in PreviewWindow._navigation_specs(stub)]
    widget.deleteLater()
    host.stop_all()
