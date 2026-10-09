"""IP14n7: single-owner MIC OUT monitoring, safety mute, persistence and mono routing."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
AUDIO = (ROOT / 'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')
REMOTE = (ROOT / 'tools/soundboard_remote/ksound_soundboard_web.py').read_text(encoding='utf-8')


def method(source, name, namespace=None):
    tree = ast.parse(source)
    matches = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(matches) == 1, (name, len(matches))
    result = {}
    exec(compile(ast.Module(body=[matches[0]], type_ignores=[]), 'extracted-method', 'exec'), namespace or {}, result)
    return result[name]


def backend():
    from ksound_hub.models import AppSettings
    settings = AppSettings.default()
    channel = next(ch for ch in settings.channels if ch.key == 'return-mic')
    channel.volume = 80
    channel.muted = False
    data = settings.to_dict()
    calls = []
    def write(doc):
        data.clear(); data.update(doc)
        settings.glass_return_mic_source = str(doc['glass_return_mic_source'])
        settings.glass_return_mic_micro_enabled = bool(doc['glass_return_mic_micro_enabled'])
        calls.append(('settings', settings.glass_return_mic_source))
    o = SimpleNamespace(
        settings=settings,
        _find_channel=lambda key: channel if key == 'return-mic' else None,
        _read_settings_document=lambda: dict(data),
        _write_settings_document=write,
        _pactl=lambda *args: calls.append(('pactl', args)),
        _unload_return_mic_source_loopbacks=lambda: calls.append(('legacy-cleanup',)),
        _save_timer=SimpleNamespace(start=lambda: calls.append(('save-queued',))),
        audio_engine=SimpleNamespace(apply_channel=lambda snapshot, key: calls.append(
            ('engine', key, snapshot.glass_return_mic_source,
             next(ch for ch in snapshot.channels if ch.key == 'return-mic').muted,
             next(ch for ch in snapshot.channels if ch.key == 'return-mic').volume,
             tuple(next(ch for ch in snapshot.channels if ch.key == 'return-mic').linked_channels))
        )),
        _audio_runtime_settings=lambda: __import__('copy').deepcopy(settings),
        _return_mic_source_config=lambda: settings.glass_return_mic_source,
        available_input_targets=lambda: [('USB Mic', 'alsa_input.usb.test')],
        resolve_input_label=lambda label: {'USB Mic': 'alsa_input.usb.test'}.get(label, ''),
        label_for_target=lambda name, **kwargs: {'alsa_input.usb.test': 'USB Mic'}.get(name, name),
        processed_micro_source_label=lambda: '🎤MICRO',
        status_changed=SimpleNamespace(emit=lambda s: calls.append(('status', s))),
    )
    o._adopt_return_mic_source_into_audio_engine = method(GLASS, '_adopt_return_mic_source_into_audio_engine').__get__(o)
    o._select_return_mic_source = method(GLASS, '_select_return_mic_source').__get__(o)
    o.set_return_mic_source_label = method(GLASS, 'set_return_mic_source_label').__get__(o)
    return o, calls, channel, data


def test_version_and_source_label_are_the_real_pipewire_source():
    assert 'APP_BUILD = "IP' in GLASS
    assert '"hub_build": "IP' in REMOTE and 'Hub IP' in REMOTE
    assert 'return_inputs = ["Off", self.backend_controller.processed_micro_source_label()]' in GLASS
    assert 'internal_sources = {"micro"}' in GLASS
    assert 'source_name=micro' in AUDIO and 'master=micro_bus.monitor' in AUDIO
    assert method(GLASS, 'processed_micro_source_label')


def test_selection_preserves_saved_volume_and_mute_and_delegates_safety_to_transport():
    o, calls, channel, data = backend()
    assert o.set_return_mic_source_label('🎤MICRO')
    assert data['glass_return_mic_source'] == 'micro'
    assert channel.volume == 80 and not channel.muted
    engines = [c for c in calls if c[0] == 'engine']
    assert len(engines) == 1, calls
    assert engines[0][2] == 'micro' and engines[0][3] is False
    assert engines[0][4] == 80
    assert engines[0][-1] == ('micro-final',)
    assert not any('set-sink-mute' in c[1] for c in calls if c[0] == 'pactl')
    assert not any('load-module' in c[1] for c in calls if c[0] == 'pactl')


def test_off_unroutes_and_does_not_duplicate_or_reconnect_soundboard():
    o, calls, channel, data = backend()
    channel.linked_channels = ['soundboard']
    assert o.set_return_mic_source_label('🎤MICRO')
    assert channel.linked_channels == ['soundboard', 'micro-final']
    count = len([c for c in calls if c[0] == 'engine'])
    assert o.set_return_mic_source_label('🎤MICRO')
    assert len([c for c in calls if c[0] == 'engine']) == count + 1  # reselect reconciles stalled route
    assert o.set_return_mic_source_label('Off')
    assert data['glass_return_mic_source'] == ''
    assert channel.linked_channels == ['soundboard']
    assert not any(c[0] == 'pactl' and 'unload-module' in c[1] for c in calls)


def test_physical_source_delegates_to_existing_engine_with_its_mono_detection():
    o, calls, channel, _data = backend()
    assert o.set_return_mic_source_label('USB Mic')
    assert channel.linked_channels == ['source:alsa_input.usb.test']
    assert not channel.muted and channel.volume == 80
    v2_method = method(AUDIO, '_ensure_return_monitor_loopback')
    text = ast.get_source_segment(AUDIO, next(n for n in ast.walk(ast.parse(AUDIO))
         if isinstance(n, ast.FunctionDef) and n.name == '_ensure_return_monitor_loopback'))
    assert v2_method and 'source_is_mono' in text
    assert 'args.extend(["channels=1", "channel_map=mono"])' in text
    assert 'args.append("channels=2")' in text
    assert '"latency_msec=60" if source_is_mono else "latency_msec=20"' in text


def test_stale_legacy_glass_route_cleanup_does_not_match_v2_or_soundboard():
    fn = method(GLASS, '_unload_return_mic_source_loopbacks')
    got=[]
    class Fake:
        def _unload_modules_matching(self, predicate):
            for line in (
                '1\tmodule-loopback\tsource=micro sink=retour sink_input_properties=media.name=K-Sound-Hub-Return-Mic-Micro',
                '2\tmodule-loopback\tsource=micro sink=retour sink_input_properties=media.name=K-Sounds-Hub-Mic-Output-Monitor',
                '3\tmodule-loopback\tsource=soundboard.monitor sink=retour',
            ):
                if predicate(line): got.append(line.split('\t')[0])
    fn(Fake())
    assert got == ['1']


def test_no_second_glass_loopback_or_double_return_mic_gain():
    compat = method(GLASS, '_load_return_mic_micro_loopback')
    source=ast.get_source_segment(GLASS, next(n for n in ast.walk(ast.parse(GLASS))
                                 if isinstance(n, ast.FunctionDef) and n.name == '_load_return_mic_micro_loopback'))
    assert compat and 'load-module' not in source and '_adopt_return_mic_source_into_audio_engine' in source
    volume = method(GLASS, '_apply_return_mic_visible_volume')
    obj=SimpleNamespace(_pactl=lambda *args: (_ for _ in ()).throw(AssertionError(args)))
    assert volume(obj) is None


def test_native_return_route_mute_restores_user_state_after_change():
    route=ast.get_source_segment(AUDIO, next(n for n in ast.walk(ast.parse(AUDIO))
                            if isinstance(n, ast.FunctionDef) and n.name == '_apply_return_mic'))
    assert '"1" if original_muted else "0"' in route
    assert '"1" if channel.muted else "0"' not in route
    assert '_cleanup_return_monitor_sources(keep_keys=desired_keys)' in route


def test_app_settings_serialization_survives_mixer_volume_save(tmp_path):
    from ksound_hub.models import AppSettings
    from ksound_hub.settings_store import SettingsStore
    o, calls, channel, _ = backend()
    o.set_return_mic_source_label('🎤MICRO')
    store=SettingsStore(tmp_path / 'settings.json')
    store.save(o.settings)
    channel.volume=10
    store.save(o.settings)
    assert store.load().glass_return_mic_source == 'micro'
    assert store.load().glass_return_mic_micro_enabled is True
    o.set_return_mic_source_label('Off')
    store.save(o.settings)
    assert store.load().glass_return_mic_source == ''


def test_v2_engine_mono_and_stereo_commands_are_distinct_without_pipewire():
    ensure = method(AUDIO, '_ensure_return_monitor_loopback')
    from types import SimpleNamespace
    calls=[]
    fake=SimpleNamespace(
        _return_monitor_media_name=lambda key: 'TestMonitor-' + key,
        _return_monitor_existing_module_ids=lambda: {},
        _source_is_mono_1ch=lambda name: name.startswith('alsa_input.'),
        _run=lambda args: (calls.append(args) or SimpleNamespace(returncode=0, stdout='41\n')),
    )
    assert ensure(fake, key='physical', source_name='alsa_input.usb.test')
    physical=calls.pop()
    assert 'channels=1' in physical and 'channel_map=mono' in physical
    assert 'channels=2' not in physical
    assert 'latency_msec=60' in physical
    assert ensure(fake, key='processed', source_name='micro')
    processed=calls.pop()
    assert 'channels=2' in processed and 'channels=1' not in processed
    assert 'latency_msec=20' in processed


def test_v2_engine_does_not_duplicate_glass_soundboard_route():
    fn = method(AUDIO, '_desired_return_monitor_sources', {
        'RETURN_MIC_MONITOR_STATIC_SOURCE_BY_KEY': {
            'soundboard': 'soundboard.monitor', 'micro-final': 'micro'
        },
        'RETURN_MIC_MONITOR_SOURCE_PREFIX': 'source:',
    })
    fake=SimpleNamespace(_source_exists=lambda name: name in {
        'soundboard.monitor', 'micro', 'alsa_input.usb.test',
    })
    channel=SimpleNamespace(linked_channels=[
        'soundboard', 'micro-final', 'source:soundboard.monitor',
        'source:alsa_input.usb.test',
    ])
    result=fn(fake, channel)
    assert result == {
        'micro-final': 'micro',
        'source:alsa_input.usb.test': 'alsa_input.usb.test',
    }


def test_ip14n4_fallback_playback_always_routes_mic_out_in_v2_engine():
    """IP14n3 regression: base apply_channel swallows return-mic as playback/EQ."""
    class Base:
        @staticmethod
        def apply_channel(engine, settings, key):
            events.append(('base', key))

    import types
    route = method(AUDIO, 'apply_channel', {
        'PipeWireAudioEngineBase': Base,
        'PLAYBACK_KEYS': ('all', 'game', 'chat', 'media', 'more', 'return-mic'),
    })
    for native_micro in (True, False):
        events = []
        fake = SimpleNamespace(
            _native_playback_enabled=lambda: False,
            _native_micro_enabled=lambda: native_micro,
            _apply_return_mic=lambda settings: events.append(('monitor', settings)),
        )
        snapshot = object()
        route(fake, snapshot, 'return-mic')
        assert events == [('monitor', snapshot)], events


def test_ip14n4_monitor_identification_is_pactl_safe_and_failure_visible():
    assert 'RETURN_MIC_MONITOR_MEDIA_PREFIX = "KSH-MICOUT-Monitor-"' in AUDIO
    assert 'mic-out-monitor-error.log' in AUDIO
    assert 'if proc.returncode != 0 or not proc.stdout.strip()' in AUDIO
