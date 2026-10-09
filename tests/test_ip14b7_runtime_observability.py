from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def test_ip14b7_build_marker_and_restart_proof():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'self._module_restart_counts: dict[str, int] = {}' in GLASS
    assert 'self._module_restart_counts[wanted] = self.module_restart_count(wanted) + 1' in GLASS
    assert 'Restarted #{restart_count}' in GLASS
    assert 'restart.setText("Working…" if busy else idle_text)' in GLASS


def test_remote_watchdog_sleeps_with_no_surface():
    queue = GLASS.split('def _queue_remote_watchdog_check', 1)[1].split('def _remote_watchdog_worker', 1)[0]
    assert 'runtime_required = bool(self.module_registry.remote_runtime_required())' in queue
    assert 'if not runtime_required and not force:' in queue
    assert 'self._remote_watchdog_timer.stop()' in queue
    assert 'self.remote_status_changed.emit("Idle")' in queue

    apply = GLASS.split('def _apply_remote_watchdog_result', 1)[1].split('def _audio_runtime_settings', 1)[0]
    assert 'elif normalized == "Idle":' in apply
    assert 'self._remote_watchdog_timer.stop()' in apply


def test_disabled_modules_report_real_runtime_policy():
    text = GLASS.split('def _module_state_text', 1)[1].split('def _refresh_module_state_row', 1)[0]
    assert 'Disabled · EQ processing bypassed' in text
    assert 'Disabled · Tune DSP bypassed · shared MICRO transport may remain' in text
    assert 'Disabled · VFX DSP bypassed · shared MICRO transport may remain' in text
    assert 'Disabled · Soundboard runtime stopped' in text
    assert 'Disabled · server stopped · watchdog asleep' in text
    assert 'Idle · server stopped · watchdog asleep' in text
