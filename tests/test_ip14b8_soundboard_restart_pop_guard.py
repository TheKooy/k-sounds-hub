from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def _function_source(name: str, next_name: str) -> str:
    return GLASS.split(f"def {name}", 1)[1].split(f"def {next_name}", 1)[0]


def test_ip14b8_exact_build_marker():
    assert 'APP_BUILD = "IP' in GLASS


def test_soundboard_restart_has_pre_and_post_silence_guard():
    assert "SOUNDBOARD_RESTART_PRE_MUTE_SECONDS = 0.03" in GLASS
    assert "SOUNDBOARD_RESTART_POST_MUTE_SECONDS = 0.12" in GLASS
    start = _function_source("_runtime_start_soundboard", "_runtime_stop_soundboard")
    stop = _function_source("_runtime_stop_soundboard", "_runtime_health_soundboard")
    assert "self._soundboard_pop_guard_active = True" in start
    assert "self._set_soundboard_bus_muted(True)" in start
    assert "self._set_soundboard_route_streams_muted(True)" in start
    assert "time.sleep(SOUNDBOARD_RESTART_POST_MUTE_SECONDS)" in start
    assert "self._unmute_soundboard_output_streams(force=True)" in start
    assert "self._set_soundboard_route_streams_muted(True)" in stop
    assert "self._set_soundboard_bus_muted(True)" in stop
    assert "time.sleep(SOUNDBOARD_RESTART_PRE_MUTE_SECONDS)" in stop


def test_pop_guard_only_mutes_soundboard_owned_paths():
    helpers = _function_source("_set_soundboard_bus_muted", "_move_soundboard_output_streams")
    assert 'set-sink-mute", "soundboard"' in helpers
    assert "K-Sound-Hub-Soundboard-To-Output" in helpers
    assert "K-Sound-Hub-Soundboard-To-Micro" in helpers
    assert "set-sink-input-mute" in helpers
    assert "set-sink-mute\", \"media" not in helpers
    assert "set-sink-mute\", \"game" not in helpers


def test_new_loopbacks_remain_muted_while_guard_is_active():
    output = _function_source("_load_soundboard_output_loopback", "_unload_soundboard_micro_loopback")
    micro = _function_source("_load_soundboard_micro_loopback", "_return_mic_source_config")
    bus = _function_source("_ensure_soundboard_bus", "_soundboard_output_config")
    assert '_soundboard_pop_guard_active' in output
    assert "self._set_soundboard_route_streams_muted(True)" in output
    assert '_soundboard_pop_guard_active' in micro
    assert "self._set_soundboard_route_streams_muted(True)" in micro
    assert '_soundboard_pop_guard_active' in bus
    assert "self._set_soundboard_bus_muted(True)" in bus
