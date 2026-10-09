#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"
MODELS = ROOT / "src" / "ksound_hub" / "models.py"


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class missing: {name}")


def _method_node(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    matches = [
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _source(text: str, node: ast.AST) -> str:
    value = ast.get_source_segment(text, node)
    assert value is not None
    return value


def _strings(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_exit_confirmation_lifecycle_contract() -> None:
    glass = GLASS.read_text(encoding="utf-8")
    models = MODELS.read_text(encoding="utf-8")
    tree = ast.parse(glass)

    assert "skip_exit_confirmation: bool = False" in models
    assert 'skip_exit_confirmation=bool(data.get("skip_exit_confirmation", False))' in models
    assert '"skip_exit_confirmation": bool(self.skip_exit_confirmation)' in models

    drawer = _class(tree, "Drawer")
    settings_node = _method_node(drawer, "_settings_page")
    settings_page = _source(glass, settings_node)
    assert "Don't ask before quitting" in _strings(settings_node)
    assert "skip_exit_confirmation" in settings_page

    preview = _class(tree, "PreviewWindow")
    confirm_node = _method_node(preview, "_confirm_window_exit")
    confirm = _source(glass, confirm_node)
    strings = _strings(confirm_node)

    assert "Quit K-Sounds Hub" in strings
    assert "Quit K-Sounds Hub?" in strings
    assert "Are you sure you want to quit?" in strings
    assert "Don't ask again" in strings

    assert "QMessageBox(self)" in confirm
    assert 'box.setWindowTitle("Quit K-Sounds Hub")' in confirm
    assert 'box.setText("Quit K-Sounds Hub?")' in confirm
    assert 'box.setInformativeText("Are you sure you want to quit?")' in confirm
    assert "QMessageBox.Yes | QMessageBox.No" in confirm
    assert "setDefaultButton(QMessageBox.No)" in confirm
    assert "setEscapeButton(QMessageBox.No)" in confirm

    # The standard Qt layout must own placement. Do not regress to poking at
    # QMessageBox private labels or its internal grid.
    assert "findChild(" not in confirm
    assert "box.layout()" not in confirm
    assert "layout.setAlignment" not in confirm
    assert "setMaximumWidth(" not in confirm
    assert "setMinimumWidth(" not in confirm

    assert 'yes_button.setObjectName("exitYesButton")' in confirm
    assert 'no_button.setObjectName("exitNoButton")' in confirm
    assert "QPushButton#exitYesButton" in confirm
    assert "QPushButton#exitNoButton" in confirm
    assert "box.exec() == QMessageBox.Yes" in confirm


if __name__ == "__main__":
    test_exit_confirmation_lifecycle_contract()
    print("exit confirmation lifecycle contract: passed")
