"""IP14n9a experiment: opt-in native MICRO -> retour, no MICRO loopback.

These isolate control logic; real acoustic latency/xruns need a device test.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CODE = (ROOT / 'src/ksound_hub/audio/pipewire_v2_final.py').read_text()
CPP = (ROOT / 'native_engine/src/micro_engine.cpp').read_text()


def extract(name, *, env=None):
    nodes = [x for x in ast.walk(ast.parse(CODE))
             if isinstance(x, ast.FunctionDef) and x.name == name]
    assert len(nodes) == 1
    scope = {'json': json, 'time': SimpleNamespace(sleep=lambda _: None), 'os': os}
    scope.update(env or {})
    exec(compile(ast.Module(body=[nodes[0]], type_ignores=[]), name, 'exec'), scope)
    return scope[name]


def harness(*, unload_success=True, mic_ready=True):
    calls = []
    route = {'micro-final': [('41', 'micro', 'retour')]}
    channel = SimpleNamespace(enabled=True, muted=False, volume=5,
                              linked_channels=['micro-final'])

    def cleanup(**kwargs):
        calls.append(('cleanup', kwargs))
        if unload_success:
            route.clear()

    def run(args):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout='Mute: yes\n', stderr='')

    obj = SimpleNamespace(
        _find_channel=lambda settings, key: channel,
        _return_mic_legacy_cleanup_done=True,
        _native_mic_out_route_allowed=False,
        _native_micro_enabled=lambda: True,
        _native_monitor_eligible=lambda ch: True,
        _write_native_micro_state=lambda settings: calls.append(
            ('native-state', obj._native_mic_out_route_allowed)),
        _ensure_native_micro_engine=lambda: (calls.append(('ensure-native',)) or mic_ready),
        _sink_exists=lambda sink: True,
        _desired_return_monitor_sources=lambda ch: {'micro-final': 'micro'},
        _return_monitor_existing_module_ids=lambda: {
            key: [record[0] for record in records] for key, records in route.items()
        },
        _return_monitor_module_records=lambda: route,
        _native_playback_enabled=lambda: False,
        _apply_return_mic_final_output_controls=lambda ch, force_mute=False: (
            calls.append(('final', force_mute)) or True),
        _run_no_fail=lambda args: calls.append(tuple(args)),
        _run=run,
        _cleanup_return_monitor_sources=cleanup,
        _ensure_return_monitor_loopback=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError('MICRO loopback was recreated')),
        _return_mic_runtime=SimpleNamespace(capture_module_id='', applied_signature=''),
    )
    return obj, calls, channel


def test_native_monitor_replaces_only_micro_loopback_before_unmute():
    obj, calls, _ch = harness()
    extract('_apply_return_mic')(obj, object())
    assert obj._native_mic_out_route_allowed
    assert obj._return_mic_expected_sources == {'native:micro': 'micro (native)'}
    assert ('native-state', False) in calls
    assert ('native-state', True) in calls
    assert calls.index(('cleanup', {'keep_keys': set()})) < calls.index(('native-state', True))
    assert calls.index(('native-state', True)) < calls.index(('pactl', 'set-sink-mute', 'retour', '0'))
    assert obj._return_mic_runtime.applied_signature


def test_failed_loopback_unload_never_enables_second_native_feed():
    obj, calls, _ch = harness(unload_success=False)
    extract('_apply_return_mic')(obj, object())
    assert obj._native_mic_out_route_allowed is False
    assert ('native-state', True) not in calls
    assert ('pactl', 'set-sink-mute', 'retour', '0') not in calls
    assert obj._return_mic_runtime.applied_signature == ''


def test_failed_native_engine_keeps_final_monitor_muted():
    obj, calls, _ch = harness(mic_ready=False)
    extract('_apply_return_mic')(obj, object())
    assert obj._native_mic_out_route_allowed is False
    assert ('final', True) in calls
    assert ('pactl', 'set-sink-mute', 'retour', '0') not in calls
    assert obj._return_mic_runtime.applied_signature == ''


def test_native_monitor_disabled_by_default_and_needs_micro_input(monkeypatch):
    enabled = extract('_native_monitor_requested')
    eligible = extract('_native_monitor_eligible')
    channel = SimpleNamespace(linked_channels=['micro-final'])
    obj = SimpleNamespace(
        _native_monitor_requested=lambda: enabled(None),
        _native_micro_enabled=lambda: True,
        _native_micro_engine_binary=lambda: SimpleNamespace(is_file=lambda: True),
    )
    monkeypatch.delenv('KSH_MIC_OUT_NATIVE_MONITOR', raising=False)
    assert not eligible(obj, channel)
    monkeypatch.setenv('KSH_MIC_OUT_NATIVE_MONITOR', '1')
    assert eligible(obj, channel)
    assert not eligible(obj, SimpleNamespace(linked_channels=['source:physical']))
    obj._native_micro_enabled = lambda: False
    assert not eligible(obj, channel)


def test_micro_dsp_written_once_then_tapped_before_micro_bus():
    assert 'state.native_monitor_enabled = parts[1] == "1"' in CPP
    # IP14n10a feeds both destinations with ONE sanitized copy. The MIC OUT
    # meter reads micro_mix directly instead of allocating return_mix = micro_mix.
    assert 'PlaybackClient::sanitize_in_place(prepared_micro_mix);' in CPP
    assert 'return_playback.write_prepared(prepared_micro_mix);' in CPP
    limiter = CPP.rindex('micro_tune.process_final_mix(micro_mix')
    prepared = CPP.rindex('PlaybackClient::sanitize_in_place(prepared_micro_mix);')
    tap = CPP.rindex('return_playback.write_prepared(prepared_micro_mix);')
    exported = CPP.rindex('micro_playback.write_prepared(prepared_micro_mix);')
    assert limiter < prepared < tap < exported
    assert 'if (!state.native_monitor_enabled && state.return_enabled' in CPP
