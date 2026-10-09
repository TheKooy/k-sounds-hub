from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ksound_hub.module_manager import ModuleManager, ModulePhase
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry
from ksound_hub.runtime_supervisor import RuntimeAdapter, RuntimeSupervisor

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def _settings(enabled=DEFAULT_MODULE_IDS):
    return SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(enabled),
    )


def test_ip14b_build_and_runtime_supervisor_are_wired():
    assert 'APP_BUILD = "IP' in GLASS
    assert "self.runtime_supervisor = RuntimeSupervisor(" in GLASS
    assert 'runtime_id="audio.micro"' in GLASS
    assert 'runtime_id="soundboard"' in GLASS
    assert 'runtime_id="remote.server"' in GLASS
    assert "restart_module_runtime" in GLASS
    assert "refresh_module_pages" in GLASS
    assert "_rebuild_navigation" in GLASS
    assert "registry = self.backend_controller.module_registry" in GLASS
    # IP14l: Hard Gate, Tune and Voice FX have distinct module-owned cards.
    # Preserve the ordering requirement, not the retired Tune/Hard Gate coupling.
    registry_pos = GLASS.index("registry = self.backend_controller.module_registry")
    gate = 'hard_gate_available = channel_key == "micro" and registry.is_enabled("hard_gate")'
    tune = 'micro_processing_callback=self._open_micro_processing_dialog if (channel_key == "micro" and registry.is_enabled("micro_tune")) else None'
    voice = 'voice_fx_callback=self._open_micro_voice_fx_dialog if (channel_key == "micro" and registry.is_enabled("voice_fx")) else None'
    for needle in (gate, tune, voice):
        assert registry_pos < GLASS.index(needle)
    assert 'hard_gate_toggle_callback=self._set_micro_hard_gate_enabled if hard_gate_available else None' in GLASS
    assert 'hard_gate_available and registry.is_enabled("micro_tune")' not in GLASS
    assert "Remote IP10t · Hub IP14" in WEB


def test_supervisor_reconciles_required_runtime_and_waits_for_health():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    supervisor = RuntimeSupervisor(manager, registry)
    running = {"value": False}
    calls: list[str] = []

    def start():
        calls.append("start")
        running["value"] = True
        return True, "ready"

    def stop():
        calls.append("stop")
        running["value"] = False
        return True, "stopped"

    supervisor.register(
        RuntimeAdapter(
            runtime_id="soundboard",
            start=start,
            stop=stop,
            health=lambda: running["value"],
            required=lambda: registry.is_enabled("pads"),
            start_timeout=0.1,
            stop_timeout=0.1,
            poll_interval=0.001,
        )
    )

    ok, _ = supervisor.reconcile("soundboard")
    assert ok is True
    assert calls == ["start"]
    assert supervisor.status("soundboard").state == "running"
    assert manager.status("pads").runtime_state == "running"

    ok, _ = manager.request_enable("pads", False)
    assert ok is True
    ok, _ = supervisor.reconcile("soundboard")
    assert ok is True
    assert calls[-1] == "stop"
    assert supervisor.status("soundboard").state == "idle"
    assert manager.status("pads").phase == ModulePhase.INSTALLED_DISABLED


def test_shared_runtime_marks_all_consumers_running():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    supervisor = RuntimeSupervisor(manager, registry)
    running = {"value": False}

    supervisor.register(
        RuntimeAdapter(
            runtime_id="audio.micro",
            start=lambda: (running.__setitem__("value", True) or True, "ready"),
            stop=lambda: (running.__setitem__("value", False) or True, "stopped"),
            health=lambda: running["value"],
            required=lambda: True,
            start_timeout=0.1,
            stop_timeout=0.1,
            poll_interval=0.001,
        )
    )
    ok, _ = supervisor.reconcile("audio.micro")
    assert ok is True
    assert manager.status("micro_tune").runtime_state == "running"
    assert manager.status("voice_fx").runtime_state == "running"


def test_restart_is_runtime_scoped_not_core_restart():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    supervisor = RuntimeSupervisor(manager, registry)
    running = {"value": True}
    calls: list[str] = []

    def stop():
        calls.append("stop")
        running["value"] = False
        return True, "stopped"

    def start():
        calls.append("start")
        running["value"] = True
        return True, "ready"

    supervisor.register(
        RuntimeAdapter(
            runtime_id="remote.server",
            start=start,
            stop=stop,
            health=lambda: running["value"],
            required=lambda: True,
            start_timeout=0.1,
            stop_timeout=0.1,
            poll_interval=0.001,
        )
    )
    ok, _ = supervisor.restart("remote.server")
    assert ok is True
    assert calls == ["stop", "start"]


def test_remote_runtime_remains_single_shared_and_conditional():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    supervisor = RuntimeSupervisor(manager, registry)
    running = {"value": True}
    starts: list[tuple[str, ...]] = []

    def start():
        starts.append(registry.remote_surfaces())
        running["value"] = True
        return True, "ready"

    def stop():
        running["value"] = False
        return True, "stopped"

    supervisor.register(
        RuntimeAdapter(
            runtime_id="remote.server",
            start=start,
            stop=stop,
            health=lambda: running["value"],
            required=registry.remote_runtime_required,
            start_timeout=0.1,
            stop_timeout=0.1,
            poll_interval=0.001,
        )
    )
    ok, _ = supervisor.reconcile("remote.server")
    assert ok is True
    assert starts[-1] == ("pads", "voice_fx")

    manager.request_enable("pads", False)
    ok, _ = supervisor.reconcile("remote.server")
    assert ok is True
    assert starts[-1] == ("voice_fx",)

    manager.request_enable("voice_fx", False)
    ok, _ = supervisor.reconcile("remote.server")
    assert ok is True
    assert running["value"] is False
    assert supervisor.status("remote.server").state == "idle"


def test_starting_phase_is_not_cleared_early_by_runtime_health():
    registry = ModuleRegistry(_settings())
    manager = ModuleManager(registry)
    assert manager.begin("voice_fx", ModulePhase.STARTING)
    manager.mark_runtime_state("voice_fx", "running")
    assert manager.status("voice_fx").phase == ModulePhase.STARTING
    manager.finish("voice_fx")
    assert manager.status("voice_fx").phase == ModulePhase.RUNNING
