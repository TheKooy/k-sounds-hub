"""IP15m3 integration: no real audio/MIDI or user modules."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from ksound_hub.module_host import ExternalModuleHost, PROTOCOL
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry
from ksound_hub.module_manager import ModuleManager, ModulePhase
from ksound_hub.module_packages import LocalModulePackageInstaller

RUNNER = '''import json, sys
print(json.dumps({"type":"hello","protocol":"ksounds-jsonl/1","id":"external_probe","version":"0.1.0"}),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 if m["command"] == "exit": sys.exit(0)
 print(json.dumps({"seq":m["seq"],"ok":True,"result":{"echo":m["command"]}}),flush=True)
'''

def rig(tmp_path, runner=RUNNER):
    settings=SimpleNamespace(installed_modules=list(DEFAULT_MODULE_IDS),enabled_modules=list(DEFAULT_MODULE_IDS))
    registry=ModuleRegistry(settings,external_root=tmp_path/'modules')
    manager=ModuleManager(registry)
    archive=tmp_path/'probe.ksmodule'
    with zipfile.ZipFile(archive,'w') as zf:
        zf.writestr('module.json',json.dumps({'id':'external_probe','name':'External Probe','version':'0.1.0','ksounds_api':1,'runner':{'protocol':PROTOCOL,'entrypoint':'main.py'}}))
        zf.writestr('main.py',runner)
    installer=LocalModulePackageInstaller(manager)
    installer.install_archive(archive)
    return registry,manager,installer,ExternalModuleHost(manager)

def test_external_opt_in_start_stop_uninstall(tmp_path):
    reg,mgr,installer,host=rig(tmp_path)
    assert not reg.is_enabled('external_probe')
    with pytest.raises(ValueError,match='enabled'):
        host.start('external_probe')
    assert mgr.request_enable('external_probe',True)[0]
    hello=host.start('external_probe')
    assert hello['id']=='external_probe'
    assert mgr.status('external_probe').runtime_state=='running'
    assert host._processes['external_probe'].request('status')['result']=={'echo':'status'}
    assert host.restart('external_probe')['version']=='0.1.0'
    host.stop('external_probe')
    assert mgr.status('external_probe').runtime_state=='stopped'
    assert mgr.request_enable('external_probe',False)[0]
    assert installer.uninstall('external_probe')

def test_external_crash_and_cleanup(tmp_path):
    reg,mgr,_,host=rig(tmp_path)
    mgr.request_enable('external_probe',True)
    host.start('external_probe')
    proc=host._processes['external_probe']
    proc.process.kill()
    proc.process.wait(timeout=2)
    assert host.crashed()==('external_probe',)
    assert host.crashed()==()
    assert mgr.status('external_probe').runtime_state=='stopped'
    host.stop_all()

def test_external_bad_hello_cannot_be_registered(tmp_path):
    reg,mgr,_,host=rig(tmp_path,runner=RUNNER.replace('external_probe','wrong_id'))
    mgr.request_enable('external_probe',True)
    with pytest.raises(RuntimeError,match='identity'):
        host.start('external_probe')
    assert not host._processes

def test_gui_navigation_before_drawer_exists(tmp_path,monkeypatch):
    pytest.importorskip('PySide6')
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from ksound_hub.glass_app import PreviewWindow
    app=QApplication.instance() or QApplication([])
    reg,manager,_,host=rig(tmp_path)
    stub=SimpleNamespace(backend_controller=SimpleNamespace(module_registry=reg))
    routes=[row[0] for row in PreviewWindow._navigation_specs(stub)]
    assert 'mixer' in routes and 'settings' in routes
    assert 'external_probe' not in routes
    host.stop_all()
