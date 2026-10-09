"""IP14n9d: native MIC OUT owns one input; mixer controls are never rewritten.

These tests exercise the actual new controller, not an extracted historical
loopback method. Acoustic latency still needs real hardware validation.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ksound_hub.audio.mic_out_native import MonitorInput, reconcile_native_mic_out, selected_input


class FakeEngine:
    def __init__(self, *, linked=None, volume=68, muted=False, playback=False,
                 old_module=False, final_ready=True, engine_ready=True):
        self.channel = SimpleNamespace(
            enabled=True, linked_channels=list(linked if linked is not None else ['micro-final']),
            volume=volume, muted=muted,
        )
        self.calls = []
        self.legacy = {'KSH-MIC-OUT': [('18', 'micro', 'retour')]} if old_module else {}
        self._return_mic_legacy_cleanup_done=True
        self._native_mic_out_route_allowed = False
        self._native_mic_out_native_sources = {}
        self._return_mic_expected_sources = {}
        self._return_mic_runtime = SimpleNamespace(capture_module_id='old', applied_signature='')
        self._mic_out_native_input_signature = None
        self.sink_ready = True
        self.playback = playback
        self.final_ready = final_ready
        self.engine_ready = engine_ready
        self.sources={'alsa_input.usb.rode'}
    def _find_channel(self, settings, key):
        assert key == 'return-mic'
        return self.channel
    def _source_exists(self, name):
        return name in self.sources
    def _native_micro_engine_binary(self):
        return SimpleNamespace(is_file=lambda: True)
    def _write_native_micro_state(self, settings):
        self.calls.append(('producer', bool(self._native_mic_out_route_allowed),
                           dict(self._native_mic_out_native_sources)))
    def _cleanup_return_monitor_sources(self):
        self.calls.append(('clean-old-monitor',))
        self.legacy.clear()
    def _return_monitor_module_records(self):
        return self.legacy
    def _return_mic_has_independent_soundboard_input(self):
        # Fake a live loopback only when the independent Soundboard input is linked.
        return 'soundboard' in self.channel.linked_channels
    def _sink_exists(self, name):
        return self.sink_ready
    def _run_no_fail(self, args):
        self.calls.append(tuple(args))
    def _native_playback_enabled(self):
        return self.playback
    def _sync_return_mic_fallback_eq(self, settings):
        self.calls.append(('ensure-eq',))
    def _apply_return_mic_final_output_controls(self, ch, force_mute=False):
        self.calls.append(('headphone',ch.volume,ch.muted,force_mute))
        return self.final_ready
    def _write_v2_state(self, settings):
        self.calls.append(('v2-state',self.channel.volume,self.channel.muted))
    def _write_v2_volume_state(self, settings):
        self.calls.append(('v2-volume',self.channel.volume,self.channel.muted))
    def _ensure_v2_engine(self):
        self.calls.append(('v2-engine',))
    def _ensure_native_micro_engine(self):
        self.calls.append(('ensure-micro',))
        return self.engine_ready


def test_processed_source_is_a_single_native_feed_and_never_a_loopback():
    engine=FakeEngine(old_module=True)
    reconcile_native_mic_out(engine,object())
    assert engine._native_mic_out_route_allowed
    assert engine._native_mic_out_native_sources == {}
    assert engine._return_mic_expected_sources == {'native:micro':'micro (native)'}
    assert ('clean-old-monitor',) in engine.calls
    assert engine.calls.index(('producer',False,{})) < engine.calls.index(('producer',True,{}))
    assert not any('module-loopback' in str(call) for call in engine.calls)
    assert engine.channel.volume==68 and not engine.channel.muted


def test_changing_source_keeps_saved_mixer_values_and_disarms_before_rearm():
    engine=FakeEngine(playback=True, volume=74)
    reconcile_native_mic_out(engine,object())
    engine.calls.clear()
    engine.channel.linked_channels=['source:alsa_input.usb.rode']
    reconcile_native_mic_out(engine,object())
    assert engine.channel.volume==74 and not engine.channel.muted
    assert engine._native_mic_out_native_sources=={
        'source:alsa_input.usb.rode':'alsa_input.usb.rode'}
    assert engine.calls.index(('producer',False,{})) < engine.calls.index((
        'producer',True,{'source:alsa_input.usb.rode':'alsa_input.usb.rode'}))
    assert ('v2-volume',74,False) in engine.calls
    assert ('pactl','set-sink-volume','retour','100%') in engine.calls
    assert not any('module-loopback' in str(c) for c in engine.calls)


def test_same_source_reconcile_and_volume_change_do_not_toggle_producer():
    engine=FakeEngine(volume=23)
    reconcile_native_mic_out(engine,object())
    engine.calls.clear()
    engine.channel.volume=77
    reconcile_native_mic_out(engine,object())
    assert engine.channel.volume==77
    assert ('headphone',77,False,False) in engine.calls
    assert not any(c==('producer',False,{}) for c in engine.calls)
    assert engine._native_mic_out_route_allowed


def test_mute_is_only_applied_to_headphone_not_to_native_feed():
    engine=FakeEngine(muted=True,volume=51)
    reconcile_native_mic_out(engine,object())
    assert engine._native_mic_out_route_allowed
    assert engine.channel.muted is True and engine.channel.volume==51
    assert ('headphone',51,True,True) in engine.calls
    assert ('headphone',51,True,False) in engine.calls


def test_missing_output_never_opens_producer():
    engine=FakeEngine(final_ready=False)
    reconcile_native_mic_out(engine,object())
    assert not engine._native_mic_out_route_allowed
    assert not engine._return_mic_runtime.applied_signature
    assert ('ensure-micro',) not in engine.calls


def test_engine_failure_disarms_and_does_not_publish_route():
    engine=FakeEngine(engine_ready=False)
    reconcile_native_mic_out(engine,object())
    assert not engine._native_mic_out_route_allowed
    assert not engine._return_mic_runtime.applied_signature
    assert engine.calls[-1] == ('producer',False,{})


def test_source_off_leaves_bus_open_for_independent_soundboard():
    engine=FakeEngine(linked=['soundboard'])
    reconcile_native_mic_out(engine,object())
    assert not engine._native_mic_out_route_allowed
    assert ('pactl','set-sink-mute','retour','0') in engine.calls
    assert not any('module-loopback' in str(c) for c in engine.calls)


def test_unavailable_device_never_falls_back_to_another_microphone():
    engine=FakeEngine(linked=['source:alsa_input.usb.missing'])
    reconcile_native_mic_out(engine,object())
    assert not engine._native_mic_out_route_allowed
    assert engine._return_mic_expected_sources=={}
    engine.sources.add('alsa_input.usb.missing')
    reconcile_native_mic_out(engine,object())
    assert engine._native_mic_out_native_sources=={
        'source:alsa_input.usb.missing':'alsa_input.usb.missing'}


def test_selection_extracts_only_monitor_input_not_soundboard_or_feedback():
    ch=SimpleNamespace(enabled=True,linked_channels=['soundboard','micro-final'])
    assert selected_input(ch)==MonitorInput('processed','micro')
    ch.linked_channels=['source:retour.monitor','source:alsa_input.usb.rode']
    assert selected_input(ch)==MonitorInput('physical','alsa_input.usb.rode')


def test_ui_selection_never_contains_persistent_volume_cap():
    from test_ip14n_processed_micro_monitor import backend
    o,calls,ch,data=backend()
    ch.volume=84
    o.set_return_mic_source_label('🎤MICRO')
    assert ch.volume==84 and data['glass_return_mic_source']=='micro'
    o.set_return_mic_source_label('USB Mic')
    assert ch.volume==84 and not ch.muted
    assert data['glass_return_mic_source']=='alsa_input.usb.test'
    assert not any(c[0]=='pactl' and c[1][0]=='set-sink-mute' for c in calls)


def test_same_source_refresh_does_not_create_another_monitor_source():
    from test_ip14n_processed_micro_monitor import backend
    o,calls,ch,data=backend()
    o.set_return_mic_source_label('🎤MICRO')
    assert o.set_return_mic_source_label('🎤MICRO')
    assert [c for c in calls if c[0]=='engine'][-1][2]=='micro'
    assert ch.volume==80


def test_changes_are_isolated_from_micro_dsp_and_old_nonexperiment_route():
    audio=Path(__file__).resolve().parents[1]/'src/ksound_hub/audio/pipewire_v2_final.py'
    source=audio.read_text()
    assert 'reconcile_native_mic_out(self, settings)' in source
    assert 'if (' in source
    import ast
    tree=ast.parse((audio.parent/'mic_out_native.py').read_text())
    calls=[n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value=='module-loopback']
    assert not calls


def test_mic_out_volume_and_selection_survive_settings_store_reload(tmp_path):
    from ksound_hub.settings_store import SettingsStore
    from test_ip14n_processed_micro_monitor import backend
    obj, calls, ch, data = backend()
    ch.volume = 73
    ch.muted = False
    obj.settings_store = SettingsStore(tmp_path / 'settings.json')
    assert obj.set_return_mic_source_label('🎤MICRO')
    restored = obj.settings_store.load()
    monitor = next(c for c in restored.channels if c.key == 'return-mic')
    assert monitor.volume == 73
    assert monitor.muted is False
    assert restored.glass_return_mic_source == 'micro'
    assert obj.set_return_mic_source_label('USB Mic')
    restored = obj.settings_store.load()
    monitor = next(c for c in restored.channels if c.key == 'return-mic')
    assert monitor.volume == 73
    assert restored.glass_return_mic_source == 'alsa_input.usb.test'
