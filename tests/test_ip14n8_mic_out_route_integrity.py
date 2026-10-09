"""IP14n8: MIC OUT ownership, fail-closed monitoring, final-only gain and EQ sync.

These tests isolate the real methods without requiring a live PipeWire daemon.
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
AUDIO = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text()
BASE = (ROOT / "src/ksound_hub/audio/pipewire.py").read_text()


def method(name, text=AUDIO, symbols=None):
    found = [node for node in ast.walk(ast.parse(text))
             if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, (name, len(found))
    env = {"json": json, "time": SimpleNamespace(sleep=lambda _: None)}
    env.update(symbols or {})
    exec(compile(ast.Module(body=[found[0]], type_ignores=[]), name, "exec"), env)
    return env[name]


def test_inventory_ignores_unowned_modules_and_reads_exact_endpoints():
    listing = "\n".join([
        "71\tmodule-loopback\tsource=micro sink=retour sink_input_properties=media.name=KSH-MICOUT-Monitor-micro-final",
        "72\tmodule-loopback\tsource=old-mic sink=retour sink_input_properties=media.name=KSH-MICOUT-Monitor-micro-final",
        "73\tmodule-loopback\tsource=soundboard.monitor sink=retour sink_input_properties=media.name=K-Sound-Hub-Soundboard-To-Output",
        "74\tmodule-loopback\tsource=micro sink=retour sink_input_properties=media.name=KSH-MICOUT-Monitor-micro-final-rogue",
        "75\tmodule-null-sink\tsink_name=retour",
    ])
    o = SimpleNamespace(_run=lambda argv: SimpleNamespace(returncode=0, stdout=listing))
    got = method("_return_monitor_module_records", symbols={"RETURN_MIC_MONITOR_MEDIA_PREFIX": "KSH-MICOUT-Monitor-"})(o)
    assert got["micro-final"] == [("71", "micro", "retour"), ("72", "old-mic", "retour")]
    assert got["micro-final-rogue"] == [("74", "micro", "retour")]
    assert "soundboard" not in got


def test_reconcile_retains_one_correct_module_and_unloads_only_mic_out_duplicates():
    calls = []
    records = {
        "micro-final": [("71", "micro", "retour"), ("72", "old-mic", "retour"),
                        ("73", "micro", "retour")],
        "source:old": [("74", "old-mic", "retour")],
    }
    o = SimpleNamespace(
        _return_mic_expected_sources={"micro-final": "micro"},
        _return_monitor_module_records=lambda: records,
        _run_no_fail=lambda argv: calls.append(tuple(argv)),
    )
    method("_cleanup_return_monitor_sources")(o, keep_keys={"micro-final"})
    assert calls == [
        ("pactl", "unload-module", "72"),
        ("pactl", "unload-module", "73"),
        ("pactl", "unload-module", "74"),
    ]


def test_wrong_surviving_source_cannot_count_as_healthy_monitor():
    o = SimpleNamespace(
        _return_monitor_media_name=lambda key: "KSH-MICOUT-Monitor-" + key,
        _return_monitor_existing_module_ids=lambda: {"micro-final": ["71"]},
        _return_monitor_module_records=lambda: {"micro-final": [("71", "wrong", "retour")]},
    )
    assert not method("_ensure_return_monitor_loopback")(
        o, key="micro-final", source_name="micro",
    )


def test_creation_error_never_opens_bus_or_commits_running_signature():
    calls = []
    channel = SimpleNamespace(volume=10, muted=True, enabled=True,
                              linked_channels=["micro-final"])
    o = SimpleNamespace(
        _find_channel=lambda settings, key: channel,
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda name: True,
        _desired_return_monitor_sources=lambda ch: {"micro-final": "micro"},
        _return_monitor_existing_module_ids=lambda: {},
        _native_playback_enabled=lambda: False,
        _apply_return_mic_final_output_controls=lambda ch, force_mute=False: (
            calls.append(("final", force_mute)) or True
        ),
        _run_no_fail=lambda argv: calls.append(tuple(argv)),
        _run=lambda argv: (calls.append(tuple(argv)) or
                           SimpleNamespace(returncode=0, stdout="Mute: yes")),
        _cleanup_return_monitor_sources=lambda **kw: calls.append(("cleanup", kw)),
        _ensure_return_monitor_loopback=lambda **kw: False,
        _return_mic_runtime=SimpleNamespace(capture_module_id="old", applied_signature="old"),
    )
    method("_apply_return_mic")(o, object())
    assert ("pactl", "set-sink-mute", "retour", "0") not in calls
    assert o._return_mic_runtime.applied_signature == ""
    assert calls[-1] == ("cleanup", {}) or ("cleanup", {}) in calls
    assert ("final", True) in calls


def test_same_key_wrong_source_stays_closed_until_replaced():
    calls = []
    channel = SimpleNamespace(volume=10, muted=True, enabled=True,
                              linked_channels=["micro-final"])
    o = SimpleNamespace(
        _find_channel=lambda settings, key: channel,
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda name: True,
        _desired_return_monitor_sources=lambda ch: {"micro-final": "micro"},
        _return_monitor_existing_module_ids=lambda: {"micro-final": ["71"]},
        _return_monitor_module_records=lambda: {"micro-final": [("71", "old-mic", "retour")]},
        _native_playback_enabled=lambda: False,
        _apply_return_mic_final_output_controls=lambda ch, force_mute=False: (
            calls.append(("final", force_mute)) or True
        ),
        _run_no_fail=lambda argv: calls.append(tuple(argv)),
        _run=lambda argv: (calls.append(tuple(argv)) or
                           SimpleNamespace(returncode=0, stdout="Mute: yes")),
        _cleanup_return_monitor_sources=lambda **kw: calls.append(("cleanup", kw)),
        _ensure_return_monitor_loopback=lambda **kw: False,
        _return_mic_runtime=SimpleNamespace(capture_module_id="", applied_signature=""),
    )
    method("_apply_return_mic")(o, object())
    assert ("final", True) in calls
    assert ("pactl", "set-sink-mute", "retour", "0") not in calls
    assert o._return_mic_runtime.applied_signature == ""


def test_fallback_eq_changes_rebuild_only_micout_after_bus_muted():
    calls = []
    channel = SimpleNamespace(stereo_width=100)
    slot = SimpleNamespace(proc=SimpleNamespace(poll=lambda: None), signature="old")
    o = SimpleNamespace(
        _native_playback_enabled=lambda: False,
        _find_channel=lambda settings, key: channel,
        eq_slots={"return-mic": slot},
        _resolve_playback_target=lambda ch: "headset-b",
        _current_profile=lambda ch: "eq-b",
        _apply_return_mic_final_output_controls=lambda ch, force_mute=False: (
            calls.append(("final", force_mute)) or True
        ),
        _run_no_fail=lambda argv: calls.append(tuple(argv)),
        _apply_eq_slot=lambda settings, key: calls.append(("eq", key)),
        _return_mic_final_playback_ids=lambda: ["51"],
    )
    eq_signature = lambda key, profile, target, stereo_width: "new"
    sync = method("_sync_return_mic_fallback_eq", symbols={"_build_eq_slot_signature": eq_signature})
    sync(o, object())
    assert calls == [
        ("final", True), ("pactl", "set-sink-mute", "retour", "1"),
        ("eq", "return-mic"),
    ]
    calls.clear()
    slot.signature = "new"
    sync(o, object())
    assert not calls


def test_eq_slot_is_not_an_extra_return_gain_stage():
    tree = ast.parse(BASE)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_apply_eq_slot")
    fragment = ast.get_source_segment(BASE, node)
    assert 'if key != "return-mic":' in fragment
    assert 'if key != "return-mic" and not bool(channel.muted)' in fragment


def test_monitor_source_rejects_feedback_and_double_capture():
    mapping = {"micro-final": "micro", "soundboard": "soundboard.monitor"}
    o = SimpleNamespace(_source_exists=lambda name: True)
    choose = method("_desired_return_monitor_sources", symbols={
        "RETURN_MIC_MONITOR_STATIC_SOURCE_BY_KEY": mapping,
        "RETURN_MIC_MONITOR_SOURCE_PREFIX": "source:",
    })
    chosen = choose(o, SimpleNamespace(linked_channels=[
        "source:retour.monitor", "micro-final", "source:micro",
        "source:micro_bus.monitor", "soundboard",
    ]))
    assert chosen == {"micro-final": "micro"}


def test_realtime_mic_out_volume_never_rebuilds_pipewire_routes():
    calls = []
    channel = SimpleNamespace(volume=5, muted=True, enabled=True)
    o = SimpleNamespace(
        _last_playback_settings=None,
        _native_playback_enabled=lambda: False,
        _find_channel=lambda settings, key: channel,
        _return_mic_expected_sources={"micro-final": "micro"},
        _return_mic_runtime=SimpleNamespace(applied_signature="valid"),
        _apply_return_mic_final_output_controls=lambda ch, force_mute=False: (
            calls.append((ch.volume, ch.muted, force_mute)) or True
        ),
        apply_channel_volume_fast=lambda *args: (_ for _ in ()).throw(
            AssertionError("full PipeWire apply on live slider")
        ),
    )
    method("apply_channel_volume_realtime", symbols={
        "PLAYBACK_KEYS": ("all", "game", "chat", "media", "more", "return-mic"),
    })(o, object(), "return-mic")
    assert calls == [(5, True, False)]
    o._return_mic_expected_sources = {}
    channel.muted = False
    method("apply_channel_volume_realtime", symbols={
        "PLAYBACK_KEYS": ("all", "game", "chat", "media", "more", "return-mic"),
    })(o, object(), "return-mic")
    assert calls[-1] == (5, False, True)
