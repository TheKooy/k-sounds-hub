"""IP14n7: a silent virtual sink cannot feed the fallback RETURN-MIC EQ capture."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

AUDIO = (Path(__file__).resolve().parents[1] / 'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')


def route_method():
    matches = [n for n in ast.walk(ast.parse(AUDIO))
               if isinstance(n, ast.FunctionDef) and n.name == '_apply_return_mic']
    assert len(matches) == 1
    result = {}
    exec(compile(ast.Module(body=[matches[0]], type_ignores=[]), 'ip14n7', 'exec'), {'json':json,'time':SimpleNamespace(sleep=lambda t:None)}, result)
    return result['_apply_return_mic']


def fake(*, bus_muted=True, channel_muted=True, route_present=False,
         fail_final=False, fail_open=False, native=False, sources=True):
    calls=[]
    channel=SimpleNamespace(volume=10,muted=channel_muted,enabled=True,
                            linked_channels=['micro-final'] if sources else [])
    def run(argv):
        calls.append(tuple(argv))
        if argv[1] == 'get-sink-mute':
            return SimpleNamespace(returncode=0,stdout='Mute: yes\n' if bus_muted else 'Mute: no\n')
        if argv[1] == 'set-sink-mute' and argv[-1] == '0' and fail_open:
            return SimpleNamespace(returncode=1,stdout='',stderr='simulated failure')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    def final(ch,*,force_mute=False):
        calls.append(('final',force_mute))
        return not fail_final
    obj=SimpleNamespace(
        _find_channel=lambda settings,key:channel,
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda name:True,
        _desired_return_monitor_sources=lambda ch: {'micro-final':'micro'} if sources else {},
        _return_monitor_existing_module_ids=lambda: {'micro-final':['42']} if route_present else {},
        _native_playback_enabled=lambda:native,
        _apply_return_mic_final_output_controls=final,
        _run_no_fail=lambda args:calls.append(tuple(args)),
        _run=run,
        _cleanup_return_monitor_sources=lambda **kwargs:calls.append(('cleanup',kwargs)),
        _ensure_return_monitor_loopback=lambda **kwargs:calls.append(('create',kwargs)) or True,
        _return_mic_has_independent_soundboard_input=lambda: False,
        _return_mic_runtime=SimpleNamespace(capture_module_id='', applied_signature=''),
    )
    return obj,calls


def test_fallback_must_open_virtual_input_only_after_muting_final_output():
    obj,calls=fake()
    route_method()(obj,object())
    opened=('pactl','set-sink-mute','retour','0')
    assert opened in calls
    assert calls.index(('final',True)) < calls.index(opened) < len(calls)-1
    assert calls[-1] == ('final',False)
    assert obj._return_mic_runtime.applied_signature


def test_muted_user_volume_stays_muted_even_if_virtual_input_is_open():
    obj,calls=fake(channel_muted=True)
    route_method()(obj,object())
    assert ('final',False) in calls
    assert ('pactl','set-sink-mute','retour','0') in calls
    assert obj._return_mic_runtime.applied_signature


def test_existing_unmuted_bus_does_not_get_muted_again_on_slider_updates():
    obj,calls=fake(bus_muted=False,route_present=True,channel_muted=False)
    route_method()(obj,object())
    assert ('pactl','set-sink-mute','retour','1') not in calls
    assert ('pactl','set-sink-mute','retour','0') not in calls
    assert calls.count(('final',False)) == 2 # before and after route reconcile


def test_no_source_keeps_virtual_bus_and_final_output_closed():
    obj,calls=fake(sources=False,channel_muted=False)
    route_method()(obj,object())
    assert ('pactl','set-sink-mute','retour','1') in calls
    assert ('pactl','set-sink-mute','retour','0') not in calls
    assert ('final',True) in calls


def test_failure_to_mute_final_never_opens_input_bus():
    obj,calls=fake(fail_final=True)
    route_method()(obj,object())
    assert ('pactl','set-sink-mute','retour','0') not in calls
    assert obj._return_mic_runtime.applied_signature == ''
    assert any(c[0]=='cleanup' for c in calls)


def test_failure_to_open_bus_never_unmutes_final_output():
    obj,calls=fake(fail_open=True)
    route_method()(obj,object())
    assert ('final',False) not in calls
    assert obj._return_mic_runtime.applied_signature == ''


def test_native_playback_is_unchanged():
    obj,calls=fake(native=True)
    obj._write_v2_state=lambda s:None
    obj._write_v2_volume_state=lambda s:None
    obj._ensure_v2_engine=lambda:None
    route_method()(obj,object())
    assert ('pactl','get-sink-mute','retour') not in calls
    assert ('final',False) not in calls and ('final',True) not in calls
