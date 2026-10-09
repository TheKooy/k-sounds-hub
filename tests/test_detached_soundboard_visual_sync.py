from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_PATH = ROOT / "src" / "ksound_hub" / "glass_app.py"
SOURCE = GLASS_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _class(name: str) -> ast.ClassDef:
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class: {name}")


def _method(class_name: str, method_name: str) -> ast.FunctionDef:
    for node in _class(class_name).body:
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            return node
    raise AssertionError(f"missing method: {class_name}.{method_name}")


def _source(node: ast.AST) -> str:
    segment = ast.get_source_segment(SOURCE, node)
    assert segment is not None
    return segment


def test_detached_window_receives_the_shared_backend_without_duplicate_controls() -> None:
    init = _method("DetachedPadsWindow", "__init__")
    argument_names = [argument.arg for argument in init.args.args]
    assert "backend_controller" in argument_names
    assert "visual_callback" not in argument_names

    detach_source = _source(_method("Drawer", "_detach_pads"))
    assert "backend_controller=self._backend_controller" in detach_source
    assert "visual_callback=" not in detach_source
    assert "show_detach=False" in _source(init)
    assert "Qt.WA_DeleteOnClose" in _source(init)

    drawer_sync_source = _source(_method("Drawer", "sync_visual_settings"))
    assert "window.isVisible()" in drawer_sync_source

    detach_source = _source(_method("Drawer", "_detach_pads"))
    assert "destroyed.connect(self._clear_detached_pads_window)" in detach_source
    clear_source = _source(_method("Drawer", "_clear_detached_pads_window"))
    assert "self._detached_pads_window = None" in clear_source

    detached_source = _source(_class("DetachedPadsWindow"))
    assert "_build_background_controls" not in detached_source
    assert "_request_visual_setting" not in detached_source
    assert "_visual_sliders" not in detached_source
    assert "detachedBgControls" not in detached_source
    assert 'QLabel("Backdrop")' not in detached_source


def test_detached_visuals_use_the_canonical_settings() -> None:
    sync_source = _source(_method("DetachedPadsWindow", "sync_visual_settings"))
    for attribute in (
        "wallpaper_enabled",
        "wallpaper_path",
        "glass_background_blur",
        "glass_background_saturation",
        "glass_background_contrast",
        "glass_background_darkness",
        "glass_opacity",
    ):
        assert attribute in sync_source

    assert "_background_source_for_settings" in sync_source

    queued_refresh = _source(
        _method("DetachedPadsWindow", "_refresh_background")
    )
    rendered_refresh = _source(
        _method("DetachedPadsWindow", "_refresh_background_now")
    )
    assert "_background_refresh_timer.start()" in queued_refresh
    assert "_adjust_background_pixmap" not in queued_refresh
    assert "_adjust_background_pixmap" in rendered_refresh


def test_visual_changes_are_fanned_out_to_both_windows() -> None:
    apply_source = _source(_method("PreviewWindow", "_apply_visual_setting"))
    assert apply_source.count("self._sync_shared_visual_settings(key)") == 8

    drawer_sync_source = _source(_method("Drawer", "sync_visual_settings"))
    assert 'getattr(self, "_detached_pads_window", None)' in drawer_sync_source
    assert "sync(settings, changed_key)" in drawer_sync_source


def test_main_and_detached_backgrounds_share_the_same_pixel_adjuster() -> None:
    helper_names = {
        node.name for node in TREE.body if isinstance(node, ast.FunctionDef)
    }
    assert "_background_source_for_settings" in helper_names
    assert "_background_factor_from_slider" in helper_names
    assert "_adjust_background_pixmap" in helper_names

    main_adjuster_source = _source(
        _method("PreviewWindow", "_saturate_background_pixmap")
    )
    assert "_adjust_background_pixmap" in main_adjuster_source


if __name__ == "__main__":
    test_detached_window_receives_the_shared_backend_without_duplicate_controls()
    test_detached_visuals_use_the_canonical_settings()
    test_visual_changes_are_fanned_out_to_both_windows()
    test_main_and_detached_backgrounds_share_the_same_pixel_adjuster()
    print("detached soundboard visual sync contract: passed")
