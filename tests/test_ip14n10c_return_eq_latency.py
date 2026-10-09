"""IP14n10c: opt-in per-slot RETURN-MIC EQ latency, not global quantum tuning."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksound_hub.builtin_modules.eq.lifecycle import (
    EQ_PIPEWIRE_LATENCY,
    EQ_RETURN_MIC_CANDIDATE_LATENCY,
    EqProcessLifecycle,
    eq_latency_for_slot,
)

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_and_all_other_eq_lanes_stay_at_stable_latency():
    assert EQ_PIPEWIRE_LATENCY == "1024/48000"
    assert EQ_RETURN_MIC_CANDIDATE_LATENCY == "768/48000"
    native = {"KSH_MIC_OUT_NATIVE_MONITOR": "1", "KSH_RETURN_MIC_EQ_LATENCY": "768/48000"}
    for key in ("all", "game", "chat", "media", "more", "micro"):
        assert eq_latency_for_slot(key, native) == EQ_PIPEWIRE_LATENCY
    for setting in ({}, {"KSH_MIC_OUT_NATIVE_MONITOR": "1"},
                    {"KSH_RETURN_MIC_EQ_LATENCY": "768/48000"},
                    {"KSH_MIC_OUT_NATIVE_MONITOR": "0", "KSH_RETURN_MIC_EQ_LATENCY": "768/48000"}):
        assert eq_latency_for_slot("return-mic", setting) == EQ_PIPEWIRE_LATENCY
    assert eq_latency_for_slot("return-mic", native) == EQ_RETURN_MIC_CANDIDATE_LATENCY
    assert eq_latency_for_slot("return-mic", {
        "KSH_MIC_OUT_NATIVE_MONITOR": "1", "KSH_RETURN_MIC_EQ_LATENCY": "1024/48000"
    }) == EQ_PIPEWIRE_LATENCY


def test_invalid_opt_in_fails_closed_before_spawn():
    with pytest.raises(ValueError, match="Invalid KSH_RETURN_MIC_EQ_LATENCY"):
        eq_latency_for_slot("return-mic", {
            "KSH_MIC_OUT_NATIVE_MONITOR": "1", "KSH_RETURN_MIC_EQ_LATENCY": "64/48000"
        })
    # Wrong/malformed option must not break independent lanes.
    assert eq_latency_for_slot("media", {
        "KSH_MIC_OUT_NATIVE_MONITOR": "1", "KSH_RETURN_MIC_EQ_LATENCY": "invalid"
    }) == EQ_PIPEWIRE_LATENCY


def test_actual_pipewire_spawn_receives_only_return_mic_request(monkeypatch, tmp_path):
    captured = []
    def fake_popen(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(pid=3000 + len(captured))

    monkeypatch.setattr("ksound_hub.builtin_modules.eq.lifecycle.subprocess.Popen", fake_popen)
    monkeypatch.setenv("KSH_MIC_OUT_NATIVE_MONITOR", "1")
    monkeypatch.setenv("KSH_RETURN_MIC_EQ_LATENCY", "768/48000")
    lifecycle = EqProcessLifecycle(
        runtime_dir=tmp_path, playback_stream_ids=lambda _key: set(),
        set_sink_volume=lambda _key, _volume: None,
    )
    for key in ("return-mic", "all", "game", "chat", "media", "more"):
        lifecycle.spawn_process(SimpleNamespace(key=key))
    assert len(captured) == 6
    for (command, opts), key in zip(captured, ("return-mic", "all", "game", "chat", "media", "more")):
        assert command == ["pipewire", "-c", "filter-chain.conf"]
        assert opts["env"]["PIPEWIRE_LATENCY"] == (
            "768/48000" if key == "return-mic" else "1024/48000"
        )
        assert opts["env"]["XDG_CONFIG_HOME"].endswith(f"{key}-eq-xdg")
        assert opts["start_new_session"] is True
    monkeypatch.delenv("KSH_RETURN_MIC_EQ_LATENCY")
    lifecycle.spawn_process(SimpleNamespace(key="return-mic"))
    assert captured[-1][1]["env"]["PIPEWIRE_LATENCY"] == "1024/48000"


def test_only_optional_monitor_latency_and_version_have_changed():
    cpp = (ROOT / "native_engine/src/micro_engine.cpp").read_text()
    assert 'constexpr int CHUNK_FRAMES = 480;' in cpp
    assert '"--latency-msec=10"' in cpp
    assert '"--latency-msec=20"' in cpp
    assert '"--process-time-msec=10"' in cpp
    assert 'APP_BUILD = "IP' in (ROOT / "src/ksound_hub/glass_app.py").read_text()
    life = (ROOT / "src/ksound_hub/builtin_modules/eq/lifecycle.py").read_text()
    assert 'env["PIPEWIRE_LATENCY"] = eq_latency_for_slot(slot.key, env)' in life


def test_audit_identifies_real_eq_process_request_without_exposing_other_env(monkeypatch, tmp_path, capsys):
    import importlib.util
    script = ROOT / "scripts/ksounds_monitor_latency_audit.py"
    spec = importlib.util.spec_from_file_location("ksh_n10c_audit", script)
    assert spec and spec.loader
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    def make(pid, key, latency):
        process = tmp_path / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(b"pipewire\0-c\0filter-chain.conf\0")
        (process / "environ").write_bytes(
            f"XDG_CONFIG_HOME=/tmp/{key}-eq-xdg\0PIPEWIRE_LATENCY={latency}\0PRIVATE_TOKEN=secret\0".encode()
        )
    make(121, "return-mic", "768/48000")
    make(122, "game", "1024/48000")
    assert audit.return_mic_eq_requests(tmp_path) == [(121, "768/48000")]
    monkeypatch.setattr(audit, "return_mic_eq_requests", lambda: [(121, "768/48000")])
    audit.print_return_mic_eq_profile()
    output = capsys.readouterr().out
    assert "PID 121: PIPEWIRE_LATENCY=768/48000" in output
    assert "secret" not in output
