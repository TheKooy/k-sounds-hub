from __future__ import annotations

import ast
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_SOURCE = ROOT / "src" / "ksound_hub" / "glass_app.py"


def _source() -> str:
    return GLASS_SOURCE.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    text = _source()
    tree = ast.parse(text)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return ast.get_source_segment(text, matches[0]) or ""


def _load_auto_palette():
    source = _source()
    tree = ast.parse(source)
    wanted = {"SOUNDBOARD_AUTO_PALETTES"}
    chunks = ["import hashlib"]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & wanted:
                chunks.append(ast.get_source_segment(source, node) or "")
        if isinstance(node, ast.FunctionDef) and node.name == "_soundboard_auto_palette":
            chunks.append(ast.get_source_segment(source, node) or "")
    namespace: dict[str, object] = {"hashlib": hashlib}
    exec(compile("\n".join(chunks), str(GLASS_SOURCE), "exec"), namespace)
    return namespace["_soundboard_auto_palette"]


def _load_eq_snapshot_helpers():
    source = _source()
    tree = ast.parse(source)
    wanted = {"_eq_snapshot_effective_profile", "_eq_snapshot_changed_channels"}
    chunks = [
        ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace: dict[str, object] = {}
    exec(compile("\n".join(chunks), str(GLASS_SOURCE), "exec"), namespace)
    return namespace["_eq_snapshot_changed_channels"]


def test_eq_history_covers_full_shared_library_and_channel_selection() -> None:
    snapshot = _function_source("eq_history_snapshot")
    restore = _function_source("restore_eq_history_snapshot")
    assert '"profiles": profiles' in snapshot
    assert '"selected": selected' in snapshot
    assert "EqProfile.from_dict(raw)" in restore
    assert "channel.eq_profiles = profiles" in restore
    assert "_eq_snapshot_changed_channels(before, restored, APP_ROUTE_KEYS)" in restore
    assert "self._queue_eq_history_apply(changed_keys, preferred)" in restore
    assert "self._save_timer.start()" in restore


def test_eq_history_rebuilds_only_channels_with_changed_effective_bands() -> None:
    changed_channels = _load_eq_snapshot_helpers()
    before = {
        "profiles": [
            {"name": "Flat", "bands": [{"frequency": 100, "gain_db": 0}]},
            {"name": "Bass", "bands": [{"frequency": 100, "gain_db": 4}]},
        ],
        "selected": {"game": "Flat", "chat": "Bass", "media": "Flat"},
    }
    after = {
        "profiles": [
            {"name": "Renamed Flat", "bands": [{"frequency": 100, "gain_db": 0}]},
            {"name": "Bass", "bands": [{"frequency": 100, "gain_db": 6}]},
        ],
        "selected": {"game": "Renamed Flat", "chat": "Bass", "media": "Renamed Flat"},
    }
    assert changed_channels(before, after, ["game", "chat", "media"]) == ["chat"]


def test_eq_history_returns_before_pipewire_rebuilds() -> None:
    restore = _function_source("restore_eq_history_snapshot")
    queue_apply = _function_source("_queue_eq_history_apply")
    flush_apply = _function_source("_flush_next_eq_history_apply")
    assert "audio_engine.apply_channel" not in restore
    assert "self._eq_history_apply_timer.start()" in queue_apply
    assert "self.audio_engine.apply_channel(self._audio_runtime_settings(), key)" in flush_apply
    assert "if pending:" in flush_apply


def test_eq_undo_redo_shortcuts_and_buttons_are_scoped_to_eq_panel() -> None:
    source = _source()
    panel = source.split("class EqPanel(QWidget):", 1)[1].split("class CenterGlyphButton", 1)[0]
    assert 'CenterGlyphButton("↶"' in panel
    assert 'CenterGlyphButton("↷"' in panel
    assert 'QKeySequence("Ctrl+Z")' in panel
    assert 'QKeySequence("Ctrl+Y")' in panel
    assert 'QKeySequence("Ctrl+Shift+Z")' in panel
    assert "Qt.WidgetWithChildrenShortcut" in panel
    assert "self._redo_stack.clear()" in panel
    assert "self._history_limit = 64" in panel


def test_one_slider_drag_records_one_history_item_on_release() -> None:
    pressed = _function_source("_eq_slider_pressed")
    released = _function_source("_eq_slider_released")
    pending = _function_source("_apply_pending_band")
    assert "self._band_drag_history_before = self._history_snapshot()" in pressed
    assert "self._apply_pending_band(record_history=False)" in released
    assert "self._record_history(before" in released
    assert "record_history and not self._band_drag_active" in pending


def test_automatic_pad_colours_are_stable_and_varied() -> None:
    palette = _load_auto_palette()
    assert palette("pad-1") == palette("pad-1")
    assert len({palette(f"pad-{index}") for index in range(32)}) >= 6


def test_folder_switch_reflows_cached_cards_without_rebuilding_them() -> None:
    switch = _function_source("_set_active_soundboard_folder")
    create = _function_source("_create_soundboard_folder")
    load = _function_source("_load_real_soundboard_pads")
    reflow = _function_source("_reflow_grid")
    assert "self._reflow_grid()" in switch
    assert "self._reload_soundboard_pads()" not in switch
    assert "self._reflow_grid()" in create
    assert "self._reload_soundboard_pads()" not in create
    assert "_slot_matches_active_folder" not in load
    assert "visible_cards" in reflow
    assert "card.setVisible(visible)" in reflow


if __name__ == "__main__":
    test_eq_history_covers_full_shared_library_and_channel_selection()
    test_eq_history_rebuilds_only_channels_with_changed_effective_bands()
    test_eq_history_returns_before_pipewire_rebuilds()
    test_eq_undo_redo_shortcuts_and_buttons_are_scoped_to_eq_panel()
    test_one_slider_drag_records_one_history_item_on_release()
    test_automatic_pad_colours_are_stable_and_varied()
    test_folder_switch_reflows_cached_cards_without_rebuilding_them()
    print("EQ undo/redo and Soundboard foundation contracts: passed")
