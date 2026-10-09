"""IP14n9b — native monitor must follow a route, not a temporary safety mute."""
from __future__ import annotations

from types import SimpleNamespace

from test_ip14n9a_native_mic_monitor import extract, harness


def test_native_route_is_independent_of_virtual_micro_source_startup():
    obj, calls, _ = harness()
    # A C++ tap needs neither `micro` source discovery nor a Pulse loopback.
    obj._desired_return_monitor_sources = lambda channel: {}
    extract("_apply_return_mic")(obj, object())
    assert obj._native_mic_out_route_allowed
    assert obj._return_mic_expected_sources == {
        "native:micro": "micro (native)"
    }
    assert all(call[0] != "pactl" or "load-module" not in call for call in calls)


def test_native_route_is_rearmed_after_temporary_safety_mute():
    obj, calls, channel = harness()
    obj._native_playback_enabled = lambda: True
    obj._write_v2_state = lambda settings: calls.append(("render-state", channel.muted))
    obj._write_v2_volume_state = lambda settings: calls.append(("render-volume", channel.muted))
    obj._ensure_v2_engine = lambda: True
    obj._write_native_micro_state = lambda settings: calls.append(
        ("native-state", obj._native_mic_out_route_allowed, channel.muted)
    )
    extract("_apply_return_mic")(obj, object())
    assert not channel.muted
    assert ("native-state", True, False) in calls
    assert ("render-state", True) in calls
    assert ("render-state", False) in calls


def test_source_transport_is_not_gated_by_headphone_mute():
    render = extract("_render_native_micro_state_text")
    mic = SimpleNamespace(key="micro", enabled=True, muted=False,
                          volume=65, linked_channels=[])
    monitor = SimpleNamespace(key="return-mic", enabled=True, muted=True,
                              volume=10, linked_channels=["micro-final"])
    obj = SimpleNamespace(
        _find_channel=lambda _settings, key: mic if key == "micro" else monitor,
        _native_micro_source_for_channel=lambda channel: "rode",
        _native_mic_out_route_allowed=True,
        _native_monitor_eligible=lambda channel: True,
        _current_profile=lambda channel: None,
        _bands_spec=lambda profile: "",
        _native_micro_soundboard_send_to_micro=lambda: False,
    )
    state = render(obj, SimpleNamespace())
    assert "native_monitor\t1\n" in state
    # The MICRO settings are unchanged by the monitor's mute.
    assert "muted\t0\n" in state
    monitor.enabled = False
    assert "native_monitor\t0\n" in render(obj, SimpleNamespace())
