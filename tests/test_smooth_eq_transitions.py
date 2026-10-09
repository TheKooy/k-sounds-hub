from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from ksound_hub.builtin_modules.eq.lifecycle import EqProcessLifecycle, hot_swap_duck_volume

ROOT = Path(__file__).resolve().parents[1]
GLASS_SOURCE = ROOT / "src" / "ksound_hub" / "glass_app.py"
PIPEWIRE_SOURCE = ROOT / "src" / "ksound_hub" / "audio" / "pipewire.py"
EQ_LIFECYCLE_SOURCE = ROOT / "src" / "ksound_hub" / "builtin_modules" / "eq" / "lifecycle.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str) -> str:
    text = _source(path)
    tree = ast.parse(text)
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return ast.get_source_segment(text, matches[0]) or ""


def test_eq_slider_coalesces_drag_and_flushes_release() -> None:
    source = _source(GLASS_SOURCE)
    assert "slider.sliderMoved.connect(lambda raw, idx=index: self._eq_band_moved(idx, raw))" in source
    assert "slider.sliderReleased.connect(lambda idx=index: self._eq_slider_released(idx))" in source
    assert "self.apply_timer.setInterval(90)" in source
    moved = _function_source(GLASS_SOURCE, "_eq_band_moved")
    released = _function_source(GLASS_SOURCE, "_eq_slider_released")
    assert "self._queue_band_change(index, raw)" in moved
    assert "self.apply_timer.stop()" in released
    assert "self._apply_pending_band(record_history=False)" in released


def test_pending_change_keeps_original_channel_and_profile() -> None:
    queue = _function_source(GLASS_SOURCE, "_queue_band_change")
    apply_pending = _function_source(GLASS_SOURCE, "_apply_pending_band")
    assert "self._current_channel_key()" in queue
    assert "self._current_profile_name()" in queue
    assert "channel_key, profile_name, index, raw = self._pending_band_change" in apply_pending
    assert "channel_key," in apply_pending
    assert "profile_name," in apply_pending


def test_duplicate_final_slider_value_is_audio_noop() -> None:
    setter = _function_source(GLASS_SOURCE, "set_eq_band_gain")
    assert "normalized_gain" in setter
    assert "< 0.001" in setter
    assert "return True" in setter


def test_currently_edited_channel_is_applied_first() -> None:
    apply_runtime = _function_source(GLASS_SOURCE, "_apply_eq_profile_runtime")
    assert "matching_channels.sort" in apply_runtime
    assert '!= preferred' in apply_runtime


def test_filter_restart_is_make_before_break_with_bounded_handover() -> None:
    source = _source(EQ_LIFECYCLE_SOURCE)
    assert "EQ_HOT_SWAP_DUCK_FACTOR = 0.80" in source
    assert "EQ_HOT_SWAP_READY_TIMEOUT_SECONDS = 0.09" in source
    assert "EQ_RESTART_FADE_DOWN" not in source
    assert "EQ_RESTART_FADE_UP" not in source

    start = _function_source(EQ_LIFECYCLE_SOURCE, "start_slot")
    spawn = "new_proc = self.spawn_process(slot)"
    wait = "self.wait_for_playback_stream(slot.key, previous_stream_ids, new_proc)"
    stop_previous = "self.terminate_process(previous_proc)"
    assert spawn in start
    assert wait in start
    assert stop_previous in start
    assert start.index(spawn) < start.index(wait) < start.index(stop_previous)
    assert "finally:" in start
    assert "self.set_sink_volume(slot.logical_sink, handover_volume)" in start


def test_handover_duck_never_silences_a_positive_channel() -> None:
    assert hot_swap_duck_volume(0) == 0
    assert hot_swap_duck_volume(1) == 1
    assert hot_swap_duck_volume(55) == 44
    assert hot_swap_duck_volume(100) == 80
    assert hot_swap_duck_volume(180) == 144


def test_failed_replacement_keeps_previous_slot_and_restores_volume() -> None:
    start = _function_source(EQ_LIFECYCLE_SOURCE, "start_slot")
    hot_swap = start.split("previous_stream_ids", 1)[1]
    except_block = start.split("except Exception:", 1)[1]
    assert "self.terminate_process(new_proc)" in except_block
    assert "slot.proc = previous_proc" not in start
    assert "slot.signature = signature" in hot_swap
    assert hot_swap.index("slot.signature = signature") > hot_swap.index("wait_for_playback_stream")


def test_hot_swap_runtime_order_and_failure_rollback() -> None:
    class FakeProc:
        def __init__(self, label: str) -> None:
            self.label = label
        def poll(self):
            return None

    def make_lifecycle(*, ready: bool):
        events: list[tuple[str, object]] = []
        new_proc = FakeProc("new")
        lifecycle = EqProcessLifecycle(
            runtime_dir=Path("/tmp/ksounds-test-eq"),
            playback_stream_ids=lambda key: events.append(("snapshot", key)) or {17},
            set_sink_volume=lambda _name, volume: events.append(("volume", volume)),
        )
        lifecycle.spawn_process = lambda slot: events.append(("spawn", slot.key)) or new_proc
        lifecycle.wait_for_playback_stream = lambda _key, _ids, proc: events.append(("ready", proc.label)) or ready
        lifecycle.terminate_process = lambda proc: proc is not None and events.append(("terminate", proc.label))
        lifecycle.stop_slot = lambda _slot: (_ for _ in ()).throw(AssertionError("audible hot-swap must not stop old slot first"))
        return lifecycle, events, new_proc

    old_proc = FakeProc("old")
    success_slot = SimpleNamespace(key="game", logical_sink="game", proc=old_proc, signature="old")
    success, success_events, success_new = make_lifecycle(ready=True)
    success.start_slot(success_slot, "new-signature", handover_volume=55)
    assert success_events == [
        ("snapshot", "game"), ("volume", 44), ("spawn", "game"),
        ("ready", "new"), ("terminate", "old"), ("volume", 55),
    ]
    assert success_slot.proc is success_new
    assert success_slot.signature == "new-signature"

    failed_slot = SimpleNamespace(key="media", logical_sink="media", proc=old_proc, signature="old")
    failed, failed_events, _failed_new = make_lifecycle(ready=False)
    try:
        failed.start_slot(failed_slot, "new-signature", handover_volume=55)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a dead replacement must fail the handover")
    assert failed_events == [
        ("snapshot", "media"), ("volume", 44), ("spawn", "media"),
        ("ready", "new"), ("terminate", "new"), ("volume", 55),
    ]
    assert failed_slot.proc is old_proc
    assert failed_slot.signature == "old"


def test_only_running_audible_sinks_use_hot_swap() -> None:
    apply_slot = _function_source(PIPEWIRE_SOURCE, "_apply_eq_slot")
    assert "not bool(channel.muted)" in apply_slot
    assert "self._sink_is_running(logical_sink)" in apply_slot
    assert "handover_volume=handover_volume" in apply_slot
