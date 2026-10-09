"""IP14n10a: keep known-safe PipeWire buffers; avoid duplicate DSP-output prep."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CPP = (ROOT / 'native_engine/src/micro_engine.cpp').read_text(encoding='utf-8')
GLASS = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
V2 = (ROOT / 'src/ksound_hub/audio/pipewire_v2_final.py').read_text(encoding='utf-8')


def test_existing_stable_buffers_and_safety_have_not_been_reduced():
    assert 'constexpr int CHUNK_FRAMES = 480;' in CPP
    assert '"--latency-msec=10"' in CPP  # capture
    assert '"--latency-msec=20"' in CPP  # playback
    assert '"--process-time-msec=10"' in CPP
    assert 'constexpr int PLAYBACK_WRITE_BUDGET_MS = 3;' in CPP
    assert 'constexpr int PLAYBACK_STALL_RESTART_MS = 750;' in CPP
    assert '"--period-ms",' in V2 and '"10",' in V2
    assert 'APP_BUILD = "IP' in GLASS


def test_cached_hot_reload_and_single_prepared_mix_shared_by_both_feeds():
    assert '::stat(path.c_str(), &fresh)' in CPP
    assert 'fresh.st_ino == stamp.st_ino' in CPP
    assert 'fresh.st_mtim.tv_nsec == stamp.st_mtim.tv_nsec' in CPP
    assert 'state_cache.refresh(state_path)' in CPP
    assert 'const MicroState& state = state_cache.state;' in CPP
    assert 'PlaybackClient::sanitize_in_place(prepared_micro_mix);' in CPP
    assert 'return_playback.write_prepared(prepared_micro_mix);' in CPP
    assert 'micro_playback.write_prepared(prepared_micro_mix);' in CPP
    assert 'return_mix = micro_mix' not in CPP
    assert 'state.native_monitor_enabled ? micro_mix : return_mix' in CPP
    assert 'stable-monitor 30s cycles=' in CPP
    assert 'monitor_backpressure_total=' in CPP


def test_native_probe_atomic_hot_reload_and_bit_identical_monitor(tmp_path):
    configured = os.environ.get('KSH_IP14N10_PROBE_BINARY', '')
    binary = Path(configured) if configured else ROOT / 'native_engine/build/ksound_native_micro_engine'
    if not binary.is_file():
        pytest.skip('Native binary not present; package install builds and runs this probe')
    state = tmp_path / 'micro_state.txt'
    state.write_text('enabled\t1\nvolume\t65\nnative_monitor\t1\n', encoding='utf-8')
    proc = subprocess.Popen(
        [str(binary), '--state', str(state), '--probe-stable-monitor'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == 'probe-ready'
        replacement = tmp_path / 'micro_state.txt.tmp'
        replacement.write_text('enabled\t1\nvolume\t70\nnative_monitor\t1\n', encoding='utf-8')
        replacement.replace(state)
        output, errors = proc.communicate(input='continue\n', timeout=5)
        assert proc.returncode == 0, (output, errors)
        assert 'stable_monitor=ok state_reads=2 cache_hits=1 identical_feeds=1' in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)
