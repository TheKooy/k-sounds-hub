from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METER_ENGINE = ROOT / "native_engine" / "src" / "meter_engine.cpp"
MICRO_ENGINE = ROOT / "native_engine" / "src" / "micro_engine.cpp"
VOICE_FX_DSP = ROOT / "native_engine" / "src" / "voice_fx_dsp.cpp"
MICRO_TUNE_DSP = ROOT / "native_engine" / "src" / "micro_tune_dsp.cpp"


def test_meter_engine_discards_backlog_and_tracks_a_monotonic_period() -> None:
    source = METER_ENGINE.read_text(encoding="utf-8")

    assert "RECENT_WINDOW_BYTES" in source
    assert "MAX_DRAIN_BYTES" in source
    assert "DRAIN_SETTLE_MS" in source
    assert "RATE * FRAME_BYTES * 30 / 1000" in source
    assert "RATE * FRAME_BYTES / 4" in source
    assert "constexpr int DRAIN_SETTLE_MS = 1" in source
    assert "append_recent(cap, tmp, size)" in source
    assert "::poll(&pfd, 1, 1)" in source
    assert "for (int i = 0; i < 8; ++i)" not in source
    assert '"--latency-msec=25"' in source
    assert "cap.left = std::min(1.0f, peak_l)" in source
    assert "cap.left * 0.55f" not in source
    assert "std::this_thread::sleep_until(next)" in source
    assert "next = now + period" in source


def test_micro_engine_is_clocked_and_cannot_block_on_playback() -> None:
    source = MICRO_ENGINE.read_text(encoding="utf-8")

    # The old implementation ignored --period-ms and let a blocking pacat pipe
    # pace the whole mixer. That inserted repeated silence chunks and stretched
    # one-second Soundboard events to several seconds under backpressure.
    assert "(void)period_ms" not in source
    assert "std::this_thread::yield()" not in source
    assert "const auto period = std::chrono::milliseconds(period_ms)" in source
    assert "std::this_thread::sleep_until(next)" in source

    assert "Both directions must be non-blocking" in source
    assert "fcntl(proc.fd, F_SETFL, flags | O_NONBLOCK)" in source
    assert "PLAYBACK_WRITE_BUDGET_MS" in source
    assert "PLAYBACK_STALL_RESTART_MS = 750" in source
    assert "std::chrono::steady_clock::now() >= deadline" in source
    assert "Keep the healthy playback client alive" in source
    deadline_branch = source.split("std::chrono::steady_clock::now() >= deadline", 1)[1].split("pollfd pfd", 1)[0]
    assert "backpressure_timed_out()" in deadline_branch
    poll_timeout_branch = source.split("if (ready <= 0)", 1)[1].split("continue;", 1)[0]
    assert "backpressure_timed_out()" in poll_timeout_branch
    assert "scheduler deadline reset after pause" in source
    resync_branch = source.split("if (now > next + period * 4)", 1)[1].split(
        "std::this_thread::sleep_until(next)", 1
    )[0]
    assert ".stop()" not in resync_branch
    assert "client.stop()" in source


def test_native_realtime_sources_compile() -> None:
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        return

    for path in (METER_ENGINE, MICRO_ENGINE):
        result = subprocess.run(
            [
                compiler,
                "-std=c++20",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-fsyntax-only",
                str(path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


def test_micro_runtime_is_clocked_by_period_not_by_the_playback_pipe(tmp_path: Path) -> None:
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        return

    binary = tmp_path / "ksound_native_micro_engine"
    compile_result = subprocess.run(
        [compiler, "-std=c++20", "-O2", str(MICRO_ENGINE), str(VOICE_FX_DSP), str(MICRO_TUNE_DSP), "-ldl", "-o", str(binary)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_parec = fake_bin / "parec"
    fake_parec.write_text(
        f"#!{sys.executable}\n"
        "import struct, sys, time\n"
        "chunk = struct.pack('f', 0.25) * 960\n"
        "try:\n"
        "    while True:\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        "        time.sleep(0.010)\n"
        "except BrokenPipeError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    fake_parec.chmod(0o755)

    captured = tmp_path / "captured.raw"
    fake_pacat = fake_bin / "pacat"
    fake_pacat.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "with open(os.environ['KSH_TEST_CAPTURE'], 'ab', buffering=0) as output:\n"
        "    while True:\n"
        "        data = sys.stdin.buffer.read(65536)\n"
        "        if not data:\n"
        "            break\n"
        "        output.write(data)\n",
        encoding="utf-8",
    )
    fake_pacat.chmod(0o755)

    state = tmp_path / "state.txt"
    state.write_text(
        "version\t2\n"
        "enabled\t1\n"
        "muted\t0\n"
        "volume\t100\n"
        "source\tfake.monitor\n"
        "hard_gate_enabled\t0\n"
        "hard_gate_threshold\t12\n"
        "return_enabled\t0\n"
        "return_muted\t0\n"
        "return_volume\t100\n"
        "return_target\tretour\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["KSH_TEST_CAPTURE"] = str(captured)
    process = subprocess.Popen(
        [
            str(binary),
            "--state",
            str(state),
            "--levels",
            str(tmp_path / "levels.json"),
            "--period-ms",
            "10",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.8)
        process.terminate()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    # Startup consumes a little of the 800 ms window. A correctly clocked 10 ms
    # engine therefore emits tens of chunks, never the hundreds produced by the
    # former unpaced loop (about 5.5x too many in the regression harness).
    chunks = captured.stat().st_size / 3840.0
    assert 20.0 <= chunks <= 140.0, f"unexpected 10 ms output cadence: {chunks:.1f} chunks"


def test_micro_playback_survives_transient_backpressure(tmp_path: Path) -> None:
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        return

    binary = tmp_path / "ksound_native_micro_engine"
    compile_result = subprocess.run(
        [compiler, "-std=c++20", "-O2", str(MICRO_ENGINE), str(VOICE_FX_DSP), str(MICRO_TUNE_DSP), "-ldl", "-o", str(binary)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_parec = fake_bin / "parec"
    fake_parec.write_text(
        f"#!{sys.executable}\n"
        "import struct, sys, time\n"
        "chunk = struct.pack('f', 0.25) * 960\n"
        "try:\n"
        "    while True:\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        "        time.sleep(0.010)\n"
        "except BrokenPipeError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    fake_parec.chmod(0o755)

    captured = tmp_path / "captured-after-backpressure.raw"
    fake_pacat = fake_bin / "pacat"
    fake_pacat.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "time.sleep(0.250)\n"
        "with open(os.environ['KSH_TEST_CAPTURE'], 'ab', buffering=0) as output:\n"
        "    while True:\n"
        "        data = sys.stdin.buffer.read(65536)\n"
        "        if not data:\n"
        "            break\n"
        "        output.write(data)\n",
        encoding="utf-8",
    )
    fake_pacat.chmod(0o755)

    state = tmp_path / "state.txt"
    state.write_text(
        "version\t2\n"
        "enabled\t1\n"
        "muted\t0\n"
        "volume\t100\n"
        "source\tfake.monitor\n"
        "hard_gate_enabled\t0\n"
        "hard_gate_threshold\t12\n"
        "return_enabled\t0\n"
        "return_muted\t0\n"
        "return_volume\t100\n"
        "return_target\tretour\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["KSH_TEST_CAPTURE"] = str(captured)
    process = subprocess.Popen(
        [
            str(binary),
            "--state",
            str(state),
            "--levels",
            str(tmp_path / "levels.json"),
            "--period-ms",
            "10",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        process.terminate()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    # The former 3 ms timeout killed and recreated pacat before this deliberately
    # slow client could ever start draining. The fixed engine keeps the healthy
    # child and drops late chunks, so playback recovers after the short pause.
    assert captured.is_file(), "playback client was restarted before it could drain"
    chunks = captured.stat().st_size / 3840.0
    assert chunks >= 10.0, f"playback did not recover after backpressure: {chunks:.1f} chunks"


def test_micro_scheduler_pause_preserves_live_capture_and_playback(tmp_path: Path) -> None:
    compiler = shutil.which("g++") or shutil.which("c++")
    if compiler is None:
        return

    binary = tmp_path / "ksound_native_micro_engine"
    compile_result = subprocess.run(
        [compiler, "-std=c++20", "-O2", str(MICRO_ENGINE), str(VOICE_FX_DSP), str(MICRO_TUNE_DSP), "-ldl", "-o", str(binary)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    spawn_log = tmp_path / "spawns.log"

    fake_parec = fake_bin / "parec"
    fake_parec.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, struct, sys, time\n"
        "with open(os.environ['KSH_TEST_SPAWNS'], 'a') as log:\n"
        "    log.write('parec\\n')\n"
        "def slow_exit(_signum, _frame):\n"
        "    time.sleep(0.080)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, slow_exit)\n"
        "chunk = struct.pack('f', 0.25) * 960\n"
        "try:\n"
        "    while True:\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        "        time.sleep(0.010)\n"
        "except BrokenPipeError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    fake_parec.chmod(0o755)

    captured = tmp_path / "captured-after-scheduler-pause.raw"
    fake_pacat = fake_bin / "pacat"
    fake_pacat.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        "with open(os.environ['KSH_TEST_SPAWNS'], 'a') as log:\n"
        "    log.write('pacat\\n')\n"
        "def slow_exit(_signum, _frame):\n"
        "    time.sleep(0.080)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, slow_exit)\n"
        "with open(os.environ['KSH_TEST_CAPTURE'], 'ab', buffering=0) as output:\n"
        "    while True:\n"
        "        data = sys.stdin.buffer.read(65536)\n"
        "        if not data:\n"
        "            break\n"
        "        output.write(data)\n",
        encoding="utf-8",
    )
    fake_pacat.chmod(0o755)

    state = tmp_path / "state.txt"
    state.write_text(
        "version\t2\n"
        "enabled\t1\n"
        "muted\t0\n"
        "volume\t100\n"
        "source\tfake-mic-source\n"
        "hard_gate_enabled\t0\n"
        "hard_gate_threshold\t12\n"
        "send\tfake-send\t1\tfake-send.monitor\t1.0\n"
        "return_enabled\t0\n"
        "return_muted\t0\n"
        "return_volume\t100\n"
        "return_target\tretour\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["KSH_TEST_CAPTURE"] = str(captured)
    environment["KSH_TEST_SPAWNS"] = str(spawn_log)
    engine_log = tmp_path / "engine.log"
    process = subprocess.Popen(
        [
            str(binary),
            "--state",
            str(state),
            "--log",
            str(engine_log),
            "--levels",
            str(tmp_path / "levels.json"),
            "--period-ms",
            "10",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.250)
        os.kill(process.pid, signal.SIGSTOP)
        time.sleep(0.120)
        os.kill(process.pid, signal.SIGCONT)
        time.sleep(0.700)
        process.terminate()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    # A scheduler delay is not a transport failure. The two capture clients
    # and single playback client must survive it instead of entering a costly
    # stop/respawn loop whose cleanup itself misses the next deadline.
    spawns = spawn_log.read_text(encoding="utf-8").splitlines()
    assert spawns.count("parec") == 2, spawns
    assert spawns.count("pacat") == 1, spawns
    assert "scheduler deadline reset after pause" in engine_log.read_text(encoding="utf-8")
    assert captured.is_file()
    assert captured.stat().st_size >= 10 * 3840


if __name__ == "__main__":
    test_meter_engine_discards_backlog_and_tracks_a_monotonic_period()
    test_micro_engine_is_clocked_and_cannot_block_on_playback()
    test_native_realtime_sources_compile()
    print("Native meter/MICRO real-time backlog contract: passed")
