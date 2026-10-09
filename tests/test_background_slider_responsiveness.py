#!/usr/bin/env python3
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ksound_hub" / "glass_app.py"

def cls(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} missing")

def method(text: str, c: ast.ClassDef, name: str) -> str:
    matches = [n for n in c.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(matches) == 1, f"{c.name}.{name}: expected one, got {len(matches)}"
    seg = ast.get_source_segment(text, matches[0])
    assert seg is not None
    return seg

def test_background_slider_responsiveness_contract() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(text)

    assert "import numpy as np" in text

    helper_start = text.index("def _adjust_background_pixmap(")
    helper_end = text.index("\ndef _send_ksh_ipc_payload", helper_start)
    helper = text[helper_start:helper_end]
    assert "np.frombuffer" in helper
    assert "astype(np.float32" in helper
    assert "np.clip" in helper
    assert "for y in range(height)" not in helper
    assert "for i in range(row, end, 4)" not in helper

    detached = cls(tree, "DetachedPadsWindow")
    detached_init = method(text, detached, "__init__")
    detached_resize = method(text, detached, "resizeEvent")
    detached_queue = method(text, detached, "_refresh_background")
    detached_render = method(text, detached, "_refresh_background_now")
    detached_sync = method(text, detached, "sync_visual_settings")

    assert "_background_refresh_timer" in detached_init
    assert "setSingleShot(True)" in detached_init
    assert "setInterval(120)" in detached_init
    assert "_background_scaled_cache" in detached_init
    assert "_refresh_background()" not in detached_resize
    assert "_background_refresh_timer.start()" in detached_queue
    assert "_background_scaled_cache" in detached_render
    assert "_adjust_background_pixmap(" in detached_render
    assert "self._background_scaled_cache = QPixmap()" in detached_sync

    preview = cls(tree, "PreviewWindow")
    preview_init = method(text, preview, "__init__")
    preview_render = method(text, preview, "_refresh_background_now")
    apply_visual = method(text, preview, "_apply_visual_setting")

    assert "_background_scaled_cache" in preview_init
    assert "_background_refresh_timer.setInterval(120)" in preview_init
    assert "_background_scaled_cache" in preview_render
    assert "self._background_source.scaled(" in preview_render

    for key in ("background_enabled", "background_path", "saturation", "contrast"):
        assert f'key == "{key}"' in apply_visual
    assert apply_visual.count("self._background_scaled_cache = QPixmap()") >= 2

    drawer = cls(tree, "Drawer")
    settings_page = method(text, drawer, "_settings_page")
    assert "if live:" in settings_page
    assert "if not sl.isSliderDown()" in settings_page
    assert "slider.sliderReleased.connect(commit_on_release)" in settings_page

    detached_sync = method(text, detached, "sync_visual_settings")
    assert 'key in {"", "blur"}' in detached_sync
    assert 'key in {"", "darkness", "glass"}' in detached_sync

    # Style-heavy controls use the same smooth policy as saturation/contrast:
    # move the handle freely, then apply once on release.
    assert '"Darkness",' in settings_page
    assert '"Black glass opacity",' in settings_page
    darkness_pos = settings_page.index('"Darkness",')
    glass_pos = settings_page.index('"Black glass opacity",')
    assert "live=False" in settings_page[darkness_pos:darkness_pos + 220]
    assert "live=False" in settings_page[glass_pos:glass_pos + 220]

if __name__ == "__main__":
    test_background_slider_responsiveness_contract()
    print("background slider responsiveness contract: passed")
