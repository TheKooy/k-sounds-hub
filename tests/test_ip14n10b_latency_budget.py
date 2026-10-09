"""IP14n10b: timing breakdown without altering working audio configuration."""
from __future__ import annotations
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CPP = (ROOT/'native_engine/src/micro_engine.cpp').read_text()
AUDIT = ROOT/'scripts/ksounds_monitor_latency_audit.py'


def test_audio_behavior_is_intentionally_unchanged():
    assert 'constexpr int CHUNK_FRAMES = 480;' in CPP
    assert CPP.count('"--latency-msec=10"') == 1
    assert CPP.count('"--latency-msec=20"') == 1
    assert '"--process-time-msec=10"' in CPP
    assert 'constexpr int PLAYBACK_WRITE_BUDGET_MS = 3;' in CPP
    assert 'return_playback.write_prepared(prepared_micro_mix);' in CPP
    assert 'micro_playback.write_prepared(prepared_micro_mix);' in CPP
    assert 'state.native_monitor_enabled ? micro_mix : return_mix' in CPP
    assert 'APP_BUILD = "IP' in (ROOT/'src/ksound_hub/glass_app.py').read_text()


def test_per_interval_latency_budget_breakdown_is_emitted_and_reset():
    for name in ('max_capture_wait_ms', 'max_mix_and_dsp_ms',
                 'max_monitor_write_ms', 'max_micro_write_ms',
                 'max_meter_export_ms', 'max_scheduler_wakeup_ms',
                 'max_capture_queued_after_read_ms'):
        assert f'" {name}="' in CPP, name
    assert 'client.max_queued_after_read_frames = 0;' in CPP
    assert 'const auto meter_export_start = std::chrono::steady_clock::now();' in CPP
    assert 'max_capture_wait_ms = 0.0;' in CPP
    assert 'max_scheduler_wakeup_ms = 0.0;' in CPP
    assert 'stable-monitor 30s cycles=' in CPP


def test_read_only_audit_can_parse_pipewire_compat_snapshots(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('audit_ip14n10b', AUDIT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'native_processes', lambda: [(888, ['engine','--state','x'])])
    monkeypatch.setattr(module, 'native_children', lambda pids: {'889', '890'})
    def lists(kind):
        return {
            'sinks': [{'index': 1, 'name': 'retour', 'latency': '2200 usec'}],
            'sources': [{'index': 5, 'name': 'RODE'}],
            'sink-inputs': [{'index': 3, 'sink': 1, 'buffer_usec': 16000,
                'sink_usec': 2500, 'properties': {'application.process.id': '889',
                'application.name': 'pacat'}}],
            'source-outputs': [{'index': 4, 'source': 5,
                'properties': {'application.process.id': '890',
                'application.name': 'parec'}}],
        }.get(kind, [])
    monkeypatch.setattr(module, 'pactl_json', lists)
    monkeypatch.setattr(module, 'print_logs', lambda native: print('logging ok'))
    module.main()
    out = capsys.readouterr().out
    assert 'buffer_usec: 16000' in out and 'sink_usec: 2500' in out
    assert 'destination sink: retour' in out
    assert 'capture source: RODE' in out
    assert 'No audio changes performed' in out


def test_audit_does_not_open_sound_devices_or_change_volume():
    code = AUDIT.read_text()
    ast.parse(code)
    for forbidden in ('set-sink-volume', 'set-sink-mute', 'set-default-sink',
        'module-loopback', 'pacat --', 'parec --', 'systemctl', 'pw-link --'):
        assert forbidden not in code
    assert '["pactl", "--format=json", "list", kind]' in code
    assert "Path('/proc')" in code


def test_native_probe_noninteractive(tmp_path):
    exe = Path(os.environ.get('KSH_IP14N10_PROBE_BINARY',
         str(ROOT/'native_engine/build/ksound_native_micro_engine')))
    if not exe.is_file():
        pytest.skip('compiled engine not available')
    state = tmp_path/'state.txt'
    state.write_text('enabled\t1\nvolume\t65\nnative_monitor\t1\n')
    proc = subprocess.Popen([str(exe), '--state', str(state), '--probe-stable-monitor'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'probe-ready'
        tmp = tmp_path/'state.new'
        tmp.write_text('enabled\t1\nvolume\t67\nnative_monitor\t1\n')
        tmp.replace(state)
        stdout, stderr = proc.communicate(input='continue\n', timeout=5)
        assert proc.returncode == 0, (stdout, stderr)
        assert 'stable_monitor=ok state_reads=2 cache_hits=1 identical_feeds=1' in stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)
