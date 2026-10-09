#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"
LAUNCHER = ROOT / "scripts" / "start_ksound_hub_v2.sh"


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class not found: {name}")


def _method(text: str, cls: ast.ClassDef, name: str) -> str:
    matches = [
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"{cls.name}.{name}: expected 1, got {len(matches)}"
    value = ast.get_source_segment(text, matches[0])
    assert value is not None
    return value


def test_single_instance_restore_contract() -> None:
    glass = GLASS.read_text(encoding="utf-8")
    launcher = LAUNCHER.read_text(encoding="utf-8")
    tree = ast.parse(glass)

    backend = _class(tree, "GlassBackendController")
    preview = _class(tree, "PreviewWindow")

    class_source = ast.get_source_segment(glass, backend)
    assert class_source is not None
    assert "restore_requested = Signal()" in class_source

    ipc = _method(glass, backend, "handle_ipc_message")
    assert 'if command == "restore":' in ipc
    assert "self.restore_requested.emit()" in ipc
    assert ipc.index('if command == "restore":') < ipc.index("_handle_soundboard_command")

    preview_init = _method(glass, preview, "__init__")
    assert "self.backend_controller.restore_requested.connect(self._focus_existing_window)" in preview_init

    focus = _method(glass, preview, "_focus_existing_window")
    assert "self.show()" in focus or "self.showNormal()" in focus
    assert "self.raise_()" in focus
    assert "self.activateWindow()" in focus

    fast = launcher.index("if try_restore; then")
    cleanup = launcher.index("cleanup_managed_processes", fast)
    assert fast < cleanup

    fast_tail = launcher[fast:cleanup]
    assert "exit 0" in fast_tail
    assert "start_soundboard_remote_service" not in fast_tail

    running = launcher.index("if is_real_instance_running; then", fast)
    running_tail = launcher[running:cleanup]
    assert "glass.activate" in running_tail
    assert 'touch "$ACTIVATION_FILE"' in running_tail
    assert "exit 0" in running_tail

    assert 'restart --project-root "$REPO_DIR" --quiet' in launcher
    assert 'soundboard_remote_runtime \
    start --project-root "$REPO_DIR" --quiet' not in launcher
    remote_call = launcher.index("start_soundboard_remote_service", cleanup)
    frontend_exec = launcher.index('exec "$PYTHON_BIN" -m "$FRONTEND_MODULE"', cleanup)
    assert cleanup < remote_call < frontend_exec

    assert fast < running < cleanup


if __name__ == "__main__":
    test_single_instance_restore_contract()
    print("single-instance restore contract: passed")
