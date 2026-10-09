from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_APP = ROOT / "src" / "ksound_hub" / "glass_app.py"

SOUNDBOARD_CONTROLLER_METHODS = {
    "_normalize_soundboard_output_key",
    "_read_soundboard_document",
    "_write_soundboard_document",
    "_pactl",
    "_soundboard_output_config",
    "_soundboard_send_to_micro_config",
}


def _controller_method_counts() -> Counter[str]:
    tree = ast.parse(GLASS_APP.read_text(encoding="utf-8"), filename=str(GLASS_APP))
    controller = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GlassBackendController"
    )
    return Counter(
        node.name
        for node in controller.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def test_soundboard_controller_methods_have_one_definition_each() -> None:
    counts = _controller_method_counts()
    assert {name: counts[name] for name in SOUNDBOARD_CONTROLLER_METHODS} == {
        name: 1 for name in SOUNDBOARD_CONTROLLER_METHODS
    }


if __name__ == "__main__":
    test_soundboard_controller_methods_have_one_definition_each()
    print("glass controller structure contract: passed")
