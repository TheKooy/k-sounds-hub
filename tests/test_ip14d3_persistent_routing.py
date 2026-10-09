from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
AUDIO = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
START = (ROOT / "scripts/start_ksound_hub_v2.sh").read_text(encoding="utf-8")
HANDOFF = (ROOT / "scripts/restart_ksound_ui_preserve_audio.sh").read_text(encoding="utf-8")
REMOTE = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def test_ip14d3_build_and_ui_handoff_are_wired():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'ui_restart_requested = Signal()' in GLASS
    assert 'restart-ui-preserve-audio' in GLASS
    assert 'self.backend_controller.ui_restart_requested.connect(self._exit_for_preserved_handoff)' in GLASS
    assert 'Restart UI (keep audio)' in GLASS
    assert 'Quit K-Sounds completely' in GLASS
    assert 'Hub IP14' in REMOTE


def test_preserve_shutdown_returns_before_audio_teardown():
    shutdown = GLASS.split('def shutdown(self, *, preserve_audio_routing: bool = False)', 1)[1].split('def _normalize_channel_key', 1)[0]
    preserve_pos = shutdown.index('if preserve_audio_routing:')
    return_pos = shutdown.index('return', preserve_pos)
    engine_shutdown_pos = shutdown.index('self.audio_engine.shutdown()')
    cleanup_pos = shutdown.index('self.cleanup_glass_audio_runtime()')
    assert preserve_pos < return_pos < engine_shutdown_pos < cleanup_pos
    assert 'stop_meter_probes' in shutdown[preserve_pos:return_pos]



def test_window_shutdown_keeps_meter_guard_before_destructive_backend():
    shutdown = GLASS.split('def _shutdown_window_runtime(self)', 1)[1].split('def closeEvent', 1)[0]
    guard = shutdown.index('self._window_runtime_shutdown = True')
    meter = shutdown.index('meter_timer.stop()')
    full = shutdown.index('backend.shutdown()')
    preserve = shutdown.index('backend.shutdown(preserve_audio_routing=True)')
    assert guard < meter < full
    assert preserve < full


def test_single_instance_fast_paths_still_precede_destructive_cleanup():
    fast = START.index('if try_restore; then')
    preserve = START.index('if preserve_audio_routing; then', fast)
    cleanup = START.index('cleanup_managed_processes', preserve)
    assert fast < preserve < cleanup

def test_full_quit_explicitly_disables_preserve_mode():
    quit_block = GLASS.split('def _quit_from_tray(self)', 1)[1].split('def _navigation_specs', 1)[0]
    assert 'self._preserve_audio_routing_on_exit = False' in quit_block
    assert 'self._ui_restart_in_progress = False' in quit_block


def test_launcher_has_preserve_mode_that_skips_managed_audio_cleanup():
    assert 'PRESERVE_AUDIO_ROUTING="${KSH_PRESERVE_AUDIO_ROUTING:-0}"' in START
    preserve = START.split('if preserve_audio_routing; then', 1)[1].split('else', 1)[0]
    assert 'cleanup_stale_runtime_files' in preserve
    assert 'cleanup_managed_processes' not in preserve
    assert 'audio-setup.sh' not in preserve
    assert 'start_ksound_native_shadow_bridge.sh' not in preserve
    assert 'if ! preserve_audio_routing; then' in START


def test_native_micro_can_adopt_existing_process_during_handoff():
    assert '_reuse_preserved_audio_runtime' in AUDIO
    assert 'def _preserved_native_micro_pid' in AUDIO
    assert 'return self._preserved_native_micro_pid() is not None' in AUDIO
    ensure = AUDIO.split('def _ensure_native_micro_engine', 1)[1].split('def _stop_native_micro_engine', 1)[0]
    assert 'if self._native_micro_transport_running():' in ensure
    assert 'return True' in ensure


def test_handoff_helper_requests_ui_restart_then_relaunches_in_preserve_mode():
    assert 'restart-ui-preserve-audio' in HANDOFF
    assert 'KSH_PRESERVE_AUDIO_ROUTING=1' in HANDOFF
    assert 'nohup "$REPO_DIR/scripts/start_ksound_hub_v2.sh"' in HANDOFF
    assert '--after-pid' in HANDOFF
