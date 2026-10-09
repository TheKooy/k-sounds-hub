#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ksound_hub" / "glass_app.py"


def _preview_class(tree: ast.AST) -> ast.ClassDef:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == "PreviewWindow":
            return node
    raise AssertionError("PreviewWindow class not found")


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    matches = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1, f"expected exactly one PreviewWindow.{name}, got {len(matches)}"
    return matches[0]


def _segment(text: str, node: ast.AST) -> str:
    value = ast.get_source_segment(text, node)
    assert value is not None
    return value


def test_close_to_tray_lifecycle_contract() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = _preview_class(tree)

    close_event = _segment(text, _method(cls, "closeEvent"))
    restore = _segment(text, _method(cls, "_restore_from_tray"))
    quit_from_tray = _segment(text, _method(cls, "_quit_from_tray"))
    apply_visual = _segment(text, _method(cls, "_apply_visual_setting"))

    assert "if (not self._allow_real_close) and self._close_to_tray_enabled():" in close_event
    assert "self._shutdown_window_runtime()" in close_event
    assert "event.accept()" in close_event

    assert "self._allow_real_close = False" in restore
    assert "self.showNormal()" in restore
    assert "self.activateWindow()" in restore

    assert "self._allow_real_close = True" in quit_from_tray
    assert "self.close()" in quit_from_tray

    marker = 'if key == "close_to_tray":'
    assert marker in apply_visual
    tail = apply_visual.split(marker, 1)[1].split("return", 1)[0]
    assert "settings.close_to_tray = bool(value)" in tail
    assert "self._set_tray_visible_for_state()" in tail


if __name__ == "__main__":
    test_close_to_tray_lifecycle_contract()
    print("close-to-tray lifecycle contract: passed")
