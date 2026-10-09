#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class missing: {name}")


def _method(text: str, cls: ast.ClassDef, name: str) -> str:
    nodes = [
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(nodes) == 1, f"{cls.name}.{name}: {len(nodes)}"
    value = ast.get_source_segment(text, nodes[0])
    assert value is not None
    return value


def test_remote_watchdog_status_contract() -> None:
    text = GLASS.read_text(encoding="utf-8")
    tree = ast.parse(text)

    backend = _class(tree, "GlassBackendController")
    drawer = _class(tree, "Drawer")
    preview = _class(tree, "PreviewWindow")

    backend_source = ast.get_source_segment(text, backend)
    assert backend_source is not None
    assert "remote_status_changed = Signal(str)" in backend_source
    assert "_remote_watchdog_result = Signal(str, str)" in backend_source
    assert 'os.environ.get("KSH_START_SOUNDBOARD_REMOTE", "1")' in backend_source
    assert "self._remote_watchdog_timer.setInterval(4000)" in backend_source

    queue = _method(text, backend, "_queue_remote_watchdog_check")
    assert "threading.Thread(" in queue
    assert 'name="ksh-remote-watchdog"' in queue
    assert "daemon=True" in queue

    worker = _method(text, backend, "_remote_watchdog_worker")
    assert 'self.runtime_supervisor.reconcile("remote.server")' in worker
    assert 'self.runtime_supervisor.status("remote.server")' in worker
    assert "self._remote_watchdog_shutdown" in worker
    assert "self._runtime_stop_remote()" in worker
    assert "remote_lan_access_status" not in worker
    assert 'emit("Online"' in worker
    assert 'emit("Idle"' in worker
    assert 'emit("Offline"' in worker

    register = _method(text, backend, "_register_runtime_units")
    assert 'runtime_id="remote.server"' in register
    assert "required=self.module_registry.remote_runtime_required" in register

    result = _method(text, backend, "_apply_remote_watchdog_result")
    assert "self._remote_watchdog_inflight = False" in result
    assert "self.remote_status_changed.emit(normalized)" in result

    shutdown = _method(text, backend, "shutdown")
    assert "self._remote_watchdog_shutdown = True" in shutdown
    assert "self._remote_watchdog_timer.stop()" in shutdown

    pads = _method(text, drawer, "_pads_page")
    assert 'QLabel("Remote: Checking")' in pads
    assert 'setObjectName("remoteStatusBadge")' in pads

    status = _method(text, drawer, "set_remote_status")
    for label in (
        "Remote: Server online",
        "Remote: Recovering",
        "Remote: Checking",
        "Remote: Offline",
    ):
        assert label in status

    preview_init = _method(text, preview, "__init__")
    assert (
        "self.backend_controller.remote_status_changed.connect("
        "self.drawer.set_remote_status)"
    ) in preview_init


if __name__ == "__main__":
    test_remote_watchdog_status_contract()
    print("Remote watchdog/status contract: passed")
