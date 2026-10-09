from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ksound_hub.audio.engine import AudioNode
from ksound_hub.audio.pipewire_v2_final import PLAYBACK_KEYS, PipeWireAudioEngine
from ksound_hub.models import AppSettings


ROOT = Path(__file__).resolve().parents[1]


def _source_resolution_engine() -> PipeWireAudioEngine:
    engine = object.__new__(PipeWireAudioEngine)
    engine.list_sources = lambda: [  # type: ignore[method-assign]
        AudioNode(name="micro", kind="source"),
        AudioNode(name="micro_bus.monitor", kind="source"),
        AudioNode(name="easyeffects_source", kind="source"),
    ]
    engine._run = lambda _args: SimpleNamespace(returncode=0, stdout="micro\n")  # type: ignore[method-assign]
    return engine


def test_virtual_micro_output_cannot_resolve_as_micro_input() -> None:
    engine = _source_resolution_engine()
    channel = SimpleNamespace(primary_target="micro")

    assert engine._physical_source_names() == ["easyeffects_source"]
    # Explicitly selecting K-Sounds' virtual output must fail closed; no
    # implicit switch to a completely different physical input is permitted.
    assert engine._resolved_micro_source_name(channel) == ""
    assert engine._native_micro_source_for_channel(channel) == ""
    channel.primary_target = "easyeffects_source"
    assert engine._native_micro_source_for_channel(channel) == "easyeffects_source"
    assert not engine._is_valid_micro_input_source_name("micro")
    assert not engine._is_valid_micro_input_source_name("micro_bus.monitor")


def test_safe_playback_mode_keeps_native_micro_dispatch() -> None:
    engine = object.__new__(PipeWireAudioEngine)
    calls: list[str] = []
    engine._native_playback_enabled = lambda: False  # type: ignore[method-assign]
    engine._native_micro_enabled = lambda: True  # type: ignore[method-assign]
    engine._apply_native_micro_channel = lambda _settings: calls.append("native")  # type: ignore[method-assign]

    PipeWireAudioEngine.apply_channel(engine, AppSettings.default(), "micro")

    assert calls == ["native"]


def test_safe_playback_startup_does_not_create_legacy_micro_loopback() -> None:
    engine = object.__new__(PipeWireAudioEngine)
    calls: list[str] = []
    engine._native_playback_enabled = lambda: False  # type: ignore[method-assign]
    engine._native_micro_enabled = lambda: True  # type: ignore[method-assign]
    engine._stop_v2_engine = lambda: calls.append("stop-playback")  # type: ignore[method-assign]
    engine._apply_eq_slot = lambda _settings, key: calls.append(f"eq:{key}")  # type: ignore[method-assign]
    engine._apply_native_micro_channel = lambda _settings: calls.append("native-micro")  # type: ignore[method-assign]

    PipeWireAudioEngine.apply_settings(engine, AppSettings.default())

    assert calls == [
        "stop-playback",
        *[f"eq:{key}" for key in PLAYBACK_KEYS],
        "native-micro",
    ]


def test_native_micro_volume_apply_removes_any_legacy_duplicate() -> None:
    settings = AppSettings.default()
    engine = object.__new__(PipeWireAudioEngine)
    calls: list[str] = []
    engine._native_micro_enabled = lambda: True  # type: ignore[method-assign]
    engine._find_channel = lambda loaded, key: next(  # type: ignore[method-assign]
        (channel for channel in loaded.channels if channel.key == key),
        None,
    )
    engine._write_native_micro_state = lambda _settings: calls.append("state")  # type: ignore[method-assign]
    engine._apply_micro_link_send_volumes = lambda _settings: calls.append("sends")  # type: ignore[method-assign]
    engine._unload_legacy_ksh_mic_physical_loopbacks = lambda: calls.append("unload")  # type: ignore[method-assign]
    engine._ensure_native_micro_engine = lambda: calls.append("ensure") or True  # type: ignore[method-assign]
    engine._apply_micro_endpoint_controls = lambda _channel: calls.append("endpoint")  # type: ignore[method-assign]
    engine._apply_return_mic = lambda _settings: calls.append("return")  # type: ignore[method-assign]

    PipeWireAudioEngine.apply_channel_volume_fast(engine, settings, "micro")

    assert calls == ["state", "sends", "unload", "ensure", "unload", "endpoint", "return"]


def test_glass_filters_and_rejects_virtual_micro_input() -> None:
    source = (ROOT / "src" / "ksound_hub" / "glass_app.py").read_text(encoding="utf-8")

    assert 'internal_sources = {"micro"}' in source
    assert 'name in internal_sources' in source
    assert 'Unsafe MICRO input rejected' in source
    assert 'self._repair_invalid_micro_input_target()' in source


def test_native_engine_has_last_resort_self_loop_guard() -> None:
    source = (ROOT / "native_engine" / "src" / "micro_engine.cpp").read_text(encoding="utf-8")

    assert 'unsafe_primary_micro_source' in source
    assert 'source_name == "micro"' in source
    assert 'state.mic_source.clear()' in source
