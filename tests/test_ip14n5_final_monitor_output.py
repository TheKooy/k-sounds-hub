"""IP14n5: level and mute must control the actual MIC OUT playback stream."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import time

ROOT = Path(__file__).resolve().parents[1]
AUDIO = (ROOT / 'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')


def extracted(name):
    matches = [n for n in ast.walk(ast.parse(AUDIO))
               if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(matches) == 1, (name, len(matches))
    dst = {}
    exec(compile(ast.Module(body=[matches[0]], type_ignores=[]), 'ip14n5-extracted', 'exec'),
         {'time': SimpleNamespace(sleep=lambda seconds: None), 'json': json}, dst)
    return dst[name]


def fake_stream(*, present=True, failing=None):
    calls=[]
    obj=SimpleNamespace(
        _return_mic_final_playback_ids=lambda: ['7301'] if present else [],
        _return_mic_final_muted=True,
        _run=lambda args: (
            calls.append(tuple(args))
            or SimpleNamespace(returncode=1 if (failing and failing(args)) else 0)
        ),
    )
    return obj,calls


def test_fallback_finds_precisely_the_real_return_mic_eq_stream():
    obj=SimpleNamespace(_find_sink_input_ids_by_media_name=lambda name:[name])
    assert extracted('_return_mic_final_playback_ids')(obj)==['K-Sound Hub RETURN-MIC EQ']


def test_muted_selected_source_cannot_be_heard_through_unmuted_eq_stream():
    obj,calls=fake_stream()
    chan=SimpleNamespace(volume=12,muted=True,enabled=True)
    assert extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert ('pactl','set-sink-input-mute','7301','1') in calls
    assert ('pactl','set-sink-input-volume','7301','12%') in calls
    assert not any(c[1]=='set-sink-input-mute' and c[-1]=='0' for c in calls)
    assert obj._return_mic_final_muted is True


def test_new_source_is_zero_and_muted_before_monitor_route_creation():
    obj,calls=fake_stream()
    chan=SimpleNamespace(volume=12,muted=True,enabled=True)
    assert extracted('_apply_return_mic_final_output_controls')(obj,chan,force_mute=True)
    assert calls[:2]==[
        ('pactl','set-sink-input-mute','7301','1'),
        ('pactl','set-sink-input-volume','7301','0%')]
    assert extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert calls[-2:] == [
        ('pactl','set-sink-input-mute','7301','1'),
        ('pactl','set-sink-input-volume','7301','12%')]


def test_manual_unmute_starts_at_zero_then_ramps_actual_final_stream():
    obj,calls=fake_stream()
    chan=SimpleNamespace(volume=12,muted=False,enabled=True)
    assert extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert calls[0:3] == [
        ('pactl','set-sink-input-mute','7301','1'),
        ('pactl','set-sink-input-volume','7301','0%'),
        ('pactl','set-sink-input-mute','7301','0')]
    assert [c[-1] for c in calls[3:]] == ['3%','6%','9%','12%']
    assert obj._return_mic_final_muted is False


def test_slider_after_unmute_changes_final_stream_not_virtual_sink():
    obj,calls=fake_stream()
    obj._return_mic_final_muted=False
    obj._return_mic_final_playback_ids_seen=('7301',)
    chan=SimpleNamespace(volume=5,muted=False,enabled=True)
    assert extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert calls==[
        ('pactl','set-sink-input-volume','7301','5%'),
        ('pactl','set-sink-input-mute','7301','0')]


def test_missing_eq_playback_stream_fails_closed():
    obj,calls=fake_stream(present=False)
    chan=SimpleNamespace(volume=12,muted=False,enabled=True)
    assert not extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert calls==[]
    assert obj._return_mic_final_muted is True


def test_failed_gain_command_fails_closed_after_mute():
    obj,calls=fake_stream(failing=lambda args: args[1]=='set-sink-input-volume')
    chan=SimpleNamespace(volume=12,muted=True,enabled=True)
    assert not extracted('_apply_return_mic_final_output_controls')(obj,chan)
    assert not any(c[1]=='set-sink-input-mute' and c[-1]=='0' for c in calls)
    assert obj._return_mic_final_muted is True


def test_fallback_routing_requires_final_eq_playback_stream():
    calls=[]
    fake=SimpleNamespace(
        _find_channel=lambda s,k:SimpleNamespace(volume=12,muted=True,enabled=True,linked_channels=['micro-final']),
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda sink:True,
        _desired_return_monitor_sources=lambda ch:{'micro-final':'micro'},
        _return_monitor_existing_module_ids=lambda:{},
        _native_playback_enabled=lambda:False,
        _apply_return_mic_final_output_controls=lambda ch,force_mute=False:False,
        _run_no_fail=lambda argv:calls.append(tuple(argv)),
        _cleanup_return_monitor_sources=lambda **kw:calls.append(('cleanup',)),
        _return_mic_runtime=SimpleNamespace(capture_module_id='old',applied_signature='old'),
    )
    extracted('_apply_return_mic')(fake,object())
    assert ('cleanup',) in calls
    assert fake._return_mic_runtime.applied_signature == ''
    assert not any('load-module' in c for c in calls)
    assert not any('set-sink-input-mute' in c for c in calls)


def test_fallback_mutes_real_stream_before_installing_loopback():
    calls=[]
    channel=SimpleNamespace(volume=12,muted=True,enabled=True,linked_channels=['micro-final'])
    fake=SimpleNamespace(
        _find_channel=lambda s,k:channel,
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda sink:True,
        _desired_return_monitor_sources=lambda ch:{'micro-final':'micro'},
        _return_monitor_existing_module_ids=lambda:{},
        _native_playback_enabled=lambda:False,
        _apply_return_mic_final_output_controls=lambda ch,force_mute=False: (calls.append(('final',force_mute)) or True),
        _run_no_fail=lambda argv:calls.append(tuple(argv)),
        _run=lambda argv: (calls.append(tuple(argv)) or SimpleNamespace(returncode=0,stdout='Mute: yes\n')),
        _cleanup_return_monitor_sources=lambda **kw:calls.append(('cleanup',kw)),
        _ensure_return_monitor_loopback=lambda **kw:calls.append(('create',kw)) or True,
        _return_mic_runtime=SimpleNamespace(capture_module_id='',applied_signature=''),
    )
    extracted('_apply_return_mic')(fake,object())
    assert calls.index(('final',True)) < next(i for i,v in enumerate(calls) if v[0]=='create')
    assert calls[-1] == ('final',False)
    assert fake._return_mic_runtime.applied_signature
    assert ('pactl','set-sink-mute','retour','1') in calls
    assert ('pactl','set-sink-mute','retour','0') in calls
    assert calls.index(('final',True)) < calls.index(('pactl','set-sink-mute','retour','0'))


def test_native_playback_does_not_apply_fallback_stream_gain():
    calls=[]
    channel=SimpleNamespace(volume=12,muted=True,enabled=True,linked_channels=['micro-final'])
    fake=SimpleNamespace(
        _find_channel=lambda s,k:channel,
        _return_mic_legacy_cleanup_done=True,
        _sink_exists=lambda sink:True,
        _desired_return_monitor_sources=lambda ch:{'micro-final':'micro'},
        _return_monitor_existing_module_ids=lambda:{'micro-final':['21']},
        _native_playback_enabled=lambda:True,
        _apply_return_mic_final_output_controls=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('fallback only')),
        _run_no_fail=lambda argv:calls.append(tuple(argv)),
        _cleanup_return_monitor_sources=lambda **kw:None,
        _ensure_return_monitor_loopback=lambda **kw:True,
        _return_mic_runtime=SimpleNamespace(capture_module_id='',applied_signature=''),
    )
    extracted('_apply_return_mic')(fake,object())
    assert ('pactl','set-sink-mute','retour','1') in calls
