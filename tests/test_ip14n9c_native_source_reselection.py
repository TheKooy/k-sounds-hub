"""IP14n9c: MIC OUT source reselection and native-only source transport.

These are deterministic control tests. Sound quality/latency still needs hardware.
"""
from __future__ import annotations

from types import SimpleNamespace

from test_ip14n9a_native_mic_monitor import extract, harness
from test_ip14n_processed_micro_monitor import backend


def test_reselect_same_micro_is_a_reconcile_request():
    obj, calls, ch, _ = backend()
    assert obj.set_return_mic_source_label('🎤MICRO')
    first = len([c for c in calls if c[0] == 'engine'])
    assert obj.set_return_mic_source_label('🎤MICRO')
    assert len([c for c in calls if c[0] == 'engine']) == first + 1
    assert not ch.muted


def test_new_source_keeps_unmuted_state_and_user_volume():
    obj, calls, ch, _ = backend()
    ch.volume = 70
    ch.muted = False
    assert obj.set_return_mic_source_label('USB Mic')
    assert ch.volume == 70
    assert ch.muted is False
    engine = [c for c in calls if c[0] == 'engine']
    assert len(engine) == 1 and engine[0][3] is False and engine[0][4] == 70
    assert not any(c[0] == 'pactl' and c[1] == ('set-sink-mute', 'retour', '1') for c in calls)
    ch.muted = True
    assert obj.set_return_mic_source_label('🎤MICRO')
    assert ch.muted is True  # preserve a mute actually chosen by the user


def test_physical_source_uses_cxx_return_transport_without_loopback():
    from test_ip14n9d_mic_out_rebuild import FakeEngine
    from ksound_hub.audio.mic_out_native import reconcile_native_mic_out
    obj = FakeEngine(linked=['source:alsa_input.usb.rode'])
    reconcile_native_mic_out(obj, object())
    assert obj._native_mic_out_route_allowed
    assert obj._native_mic_out_native_sources == {
        'source:alsa_input.usb.rode': 'alsa_input.usb.rode'
    }
    assert not any('module-loopback' in str(c) for c in obj.calls)


def test_physical_to_processed_switch_disables_old_feed_before_rearm():
    from test_ip14n9d_mic_out_rebuild import FakeEngine
    from ksound_hub.audio.mic_out_native import reconcile_native_mic_out
    obj = FakeEngine(linked=['source:alsa_input.usb.rode'])
    reconcile_native_mic_out(obj, object())
    obj.calls.clear()
    obj.channel.linked_channels = ['micro-final']
    reconcile_native_mic_out(obj, object())
    assert obj._native_mic_out_native_sources == {}
    assert obj._native_mic_out_route_allowed
    assert obj.calls.index(('producer', False, {})) < obj.calls.index(('producer', True, {}))
    assert not any('module-loopback' in str(c) for c in obj.calls)


def test_native_physical_route_state_is_separate_from_processed_micro():
    render = extract('_render_native_micro_state_text')
    micro = SimpleNamespace(key='micro', enabled=True, muted=False,
                            volume=65, linked_channels=[])
    monitor = SimpleNamespace(key='return-mic', enabled=True, muted=False,
                              volume=10, linked_channels=['source:alsa_input.usb.test'])
    obj = SimpleNamespace(
        _find_channel=lambda settings, key: micro if key == 'micro' else monitor,
        _native_micro_source_for_channel=lambda channel: 'rode',
        _native_mic_out_route_allowed=True,
        _native_mic_out_native_sources={'source:rode': 'alsa_input.usb.test'},
        _native_monitor_eligible=lambda channel: False,
        _current_profile=lambda channel: None,
        _bands_spec=lambda profile: '',
        _native_micro_soundboard_send_to_micro=lambda: False,
    )
    state = render(obj, SimpleNamespace())
    assert 'native_monitor\t0\n' in state
    assert 'return_enabled\t1\n' in state
    assert 'return_source\tsource:rode\t1\talsa_input.usb.test\t1.0000\n' in state
    assert 'return_volume\t100\n' in state
    monitor.enabled = False
    state = render(obj, SimpleNamespace())
    assert 'return_enabled\t0\n' in state
    assert 'return_source\t' not in state
