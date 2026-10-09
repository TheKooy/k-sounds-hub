"""MIC OUT native monitoring controller (IP14n9d).

This component owns exactly one MIC OUT input into the already-existing
``retour`` bus. It never creates a PipeWire module-loopback or mutates the
return-mic channel's saved volume/mute. The V2 final renderer (or its fallback
EQ stream) alone controls audible volume; the C++ MICRO engine sends at unity.

The Soundboard -> retour connection is not owned here and is never unloaded.
The original MIC OUT implementation remains as a non-experimental fallback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MonitorInput:
    kind: str   # off | processed | physical | unavailable
    name: str = ""


def selected_input(channel: Any) -> MonitorInput:
    """Use the existing ONE-input selector; ignore unrelated Soundboard links."""
    if channel is None or not bool(getattr(channel, 'enabled', False)):
        return MonitorInput('off')
    for item in (getattr(channel, 'linked_channels', None) or []):
        key = str(item).strip()
        if key.lower() in {'micro-final', 'micro', 'source:micro'}:
            return MonitorInput('processed', 'micro')
        if key.lower().startswith('source:'):
            name = key.partition(':')[2].strip()
            if name and name not in {
                'micro', 'micro_bus.monitor', 'retour', 'retour.monitor',
                'soundboard.monitor',
            }:
                return MonitorInput('physical', name)
        # Legacy plain source strings are intentionally NOT automatically
        # reinterpreted as arbitrary inputs: only the selector creates source:.
    return MonitorInput('off')


def _disarm(engine: Any, settings: Any) -> None:
    engine._native_mic_out_route_allowed = False
    engine._native_mic_out_native_sources = {}
    engine._return_mic_expected_sources = {}
    engine._write_native_micro_state(settings)


def reconcile_native_mic_out(engine: Any, settings: Any) -> None:
    """Reconcile from saved desired state; never use a temporary saved mute.

    Source and output gain are independent: switching and reselecting inputs
    leaves ``channel.volume`` and ``channel.muted`` unchanged. The C++ producer
    is disarmed BEFORE stale monitor loopbacks are removed and re-armed only
    after output state has been installed. Ordinary volume/mute applies only
    update the endpoint and do not dismantle the producer.
    """
    channel = engine._find_channel(settings, 'return-mic')
    if channel is None:
        _disarm(engine, settings)
        return

    wanted = selected_input(channel)
    if wanted.kind == 'physical' and not engine._source_exists(wanted.name):
        wanted = MonitorInput('unavailable', wanted.name)
    if wanted.kind == 'processed' and not engine._native_micro_engine_binary().is_file():
        wanted = MonitorInput('unavailable', 'micro')

    # This is the *only* startup cleanup for legacy MIC OUT monitor routes.
    # _cleanup_return_monitor_sources matches only the monitor-specific media
    # prefix; it never removes Soundboard -> retour or MICRO -> micro_bus.
    if not engine._return_mic_legacy_cleanup_done:
        _disarm(engine, settings)
        engine._disconnect_return_playback_links()
        engine._cleanup_legacy_return_playback_modules()
        for module_id in engine._find_loopback_module_ids_by_media_name(
            'K-Sounds Hub Mic Output Monitor Capture'
        ):
            engine._run_no_fail(['pactl', 'unload-module', module_id])
        engine._return_mic_legacy_cleanup_done = True

    # Ensure that there is no secondary MIC OUT producer, even if it survived
    # an earlier crashed instance. Other modules/soundboard are not touched.
    engine._cleanup_return_monitor_sources()
    if engine._return_monitor_module_records():
        _disarm(engine, settings)
        engine._return_mic_runtime.applied_signature = ''
        return

    sink_ready = engine._sink_exists('retour')
    if not sink_ready:
        engine._run_no_fail([
            'pactl', 'load-module', 'module-null-sink', 'sink_name=retour',
            'sink_properties=device.description=🎧MIC OUT',
        ])
        sink_ready = engine._sink_exists('retour')
    if not sink_ready:
        _disarm(engine, settings)
        engine._return_mic_runtime.applied_signature = ''
        return

    # Independent Soundboard -> retour may be the ONLY selected MIC OUT input.
    # Validate the actual Glass loopback, not just the saved dropdown choice.
    soundboard_input = engine._return_mic_has_independent_soundboard_input()
    has_input = wanted.kind in {'physical', 'processed'} or soundboard_input

    target_signature = f'{wanted.kind}:{wanted.name}'
    prior_signature = getattr(engine, '_mic_out_native_input_signature', None)
    changing = prior_signature != target_signature
    if changing:
        # No intermediate model/slider change, no volume cap. Disarm first so
        # a source switch cannot overlap stale and new monitored signals.
        _disarm(engine, settings)

    native_playback = engine._native_playback_enabled()
    if native_playback:
        engine._write_v2_state(settings)
        engine._write_v2_volume_state(settings)
        engine._ensure_v2_engine()
    else:
        engine._sync_return_mic_fallback_eq(settings)
        # In fallback, do not open the producer if there is no controllable
        # final headphone stream. Existing stream is always gain/mute-synced.
        if not engine._apply_return_mic_final_output_controls(channel, force_mute=changing or not has_input):
            _disarm(engine, settings)
            engine._return_mic_runtime.applied_signature = ''
            return

    if wanted.kind == 'processed':
        engine._native_mic_out_route_allowed = True
        engine._native_mic_out_native_sources = {}
        expected = {'native:micro': 'micro (native)'}
    elif wanted.kind == 'physical':
        engine._native_mic_out_route_allowed = True
        engine._native_mic_out_native_sources = {
            f'source:{wanted.name}': wanted.name,
        }
        expected = {f'native:source:{wanted.name}': wanted.name}
    else:
        engine._native_mic_out_route_allowed = False
        engine._native_mic_out_native_sources = {}
        expected = {}

    engine._write_native_micro_state(settings)
    if expected and not engine._ensure_native_micro_engine():
        _disarm(engine, settings)
        engine._return_mic_runtime.applied_signature = ''
        return

    # retour is a unity mixer. Its output stream, not the bus, owns MIC OUT
    # volume/mute. Do not leave the bus muted after switching sources; it also
    # carries independent Soundboard audio. If user muted MIC OUT, the actual
    # final playback stage remains muted by its own state.
    engine._run_no_fail(['pactl', 'set-sink-volume', 'retour', '100%'])
    engine._run_no_fail(['pactl', 'set-sink-mute', 'retour', '0' if has_input else '1'])
    if not native_playback:
        if not engine._apply_return_mic_final_output_controls(channel, force_mute=not has_input):
            _disarm(engine, settings)
            engine._return_mic_runtime.applied_signature = ''
            return

    engine._return_mic_expected_sources = expected
    engine._mic_out_native_input_signature = target_signature
    engine._return_mic_runtime.capture_module_id = ''
    engine._return_mic_runtime.applied_signature = json.dumps(
        {'transport': 'native', 'input': target_signature}, sort_keys=True,
    )
