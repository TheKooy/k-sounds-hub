from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .module_manager import ModuleManager
from .module_registry import ModuleRegistry

RuntimeAction = Callable[[], tuple[bool, str] | bool | None]
RuntimeHealth = Callable[[], bool]
RuntimeRequired = Callable[[], bool]
RuntimeMessage = Callable[[str, str], None]


def _normalize_action_result(value, *, success_message: str) -> tuple[bool, str]:
    if isinstance(value, tuple) and len(value) >= 2:
        return bool(value[0]), str(value[1] or success_message)
    if value is None:
        return True, success_message
    return bool(value), success_message if bool(value) else "Runtime action failed"


@dataclass
class RuntimeAdapter:
    runtime_id: str
    start: RuntimeAction
    stop: RuntimeAction
    health: RuntimeHealth
    required: RuntimeRequired
    start_timeout: float = 4.5
    stop_timeout: float = 2.5
    poll_interval: float = 0.08
    description: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    runtime_id: str
    required: bool
    state: str
    healthy: bool
    detail: str = ""


class RuntimeSupervisor:
    """Own runtime lifecycle independently from module install/enable state.

    The supervisor never decides whether a module is installed or enabled; that
    remains ModuleManager/ModuleRegistry territory. It only reconciles concrete
    runtime units to the desired state and reports health back to every enabled
    module consuming that runtime.
    """

    def __init__(
        self,
        manager: ModuleManager,
        registry: ModuleRegistry | None = None,
        *,
        state_callback: RuntimeMessage | None = None,
    ) -> None:
        self.manager = manager
        self.registry = registry or manager.registry
        self.state_callback = state_callback
        self._adapters: dict[str, RuntimeAdapter] = {}
        self._states: dict[str, RuntimeStatus] = {}

    @staticmethod
    def _key(runtime_id: str) -> str:
        return str(runtime_id or "").strip().lower()

    def register(self, adapter: RuntimeAdapter) -> None:
        runtime_id = self._key(adapter.runtime_id)
        if not runtime_id:
            raise ValueError("runtime id is required")
        if runtime_id in self._adapters:
            raise ValueError(f"runtime already registered: {runtime_id}")
        adapter.runtime_id = runtime_id
        self._adapters[runtime_id] = adapter
        self._set_state(runtime_id, "unconfirmed", False, "Registered")

    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def adapter(self, runtime_id: str) -> RuntimeAdapter | None:
        return self._adapters.get(self._key(runtime_id))

    def runtimes_for_module(self, module_id: str) -> tuple[str, ...]:
        manifest = self.registry.manifest(module_id)
        if manifest is None:
            return ()
        return tuple(runtime for runtime in manifest.runtimes if runtime in self._adapters)

    def modules_for_runtime(self, runtime_id: str) -> tuple[str, ...]:
        wanted = self._key(runtime_id)
        result: list[str] = []
        for manifest in self.registry.manifests():
            if wanted in manifest.runtimes:
                result.append(manifest.module_id)
        return tuple(result)

    def _mark_consumers(self, runtime_id: str, state: str) -> None:
        for module_id in self.modules_for_runtime(runtime_id):
            self.manager.mark_runtime_state(module_id, state)

    def _set_state(self, runtime_id: str, state: str, healthy: bool, detail: str = "") -> None:
        wanted = self._key(runtime_id)
        adapter = self._adapters.get(wanted)
        required = bool(adapter.required()) if adapter is not None else False
        status = RuntimeStatus(
            runtime_id=wanted,
            required=required,
            state=str(state or "unknown"),
            healthy=bool(healthy),
            detail=str(detail or ""),
        )
        self._states[wanted] = status
        module_state = {
            "running": "running",
            "starting": "starting",
            "stopping": "stopping",
            "idle": "idle",
            "stopped": "stopped",
            "error": "error",
        }.get(status.state, "unknown")
        self._mark_consumers(wanted, module_state)
        if self.state_callback is not None:
            try:
                self.state_callback(wanted, status.state)
            except Exception:
                pass

    def status(self, runtime_id: str) -> RuntimeStatus:
        wanted = self._key(runtime_id)
        adapter = self._adapters.get(wanted)
        if adapter is None:
            return RuntimeStatus(wanted, False, "unavailable", False, "Runtime not registered")
        current = self._states.get(wanted)
        try:
            healthy = bool(adapter.health())
        except Exception as exc:
            self._set_state(wanted, "error", False, str(exc))
            return self._states[wanted]
        if current is None or current.state not in {"starting", "stopping", "error"}:
            if adapter.required():
                self._set_state(
                    wanted,
                    "running" if healthy else "stopped",
                    healthy,
                    "Healthy" if healthy else "Not running",
                )
            else:
                self._set_state(
                    wanted,
                    "idle" if not healthy else "running",
                    healthy,
                    "Not required" if not healthy else "Running although not required",
                )
        return self._states[wanted]

    @staticmethod
    def _wait_for_health(adapter: RuntimeAdapter, wanted: bool, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                current = bool(adapter.health())
            except Exception:
                current = False
            if current is wanted:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(max(0.01, float(adapter.poll_interval)))

    def reconcile(self, runtime_id: str) -> tuple[bool, str]:
        wanted = self._key(runtime_id)
        adapter = self._adapters.get(wanted)
        if adapter is None:
            return False, f"Runtime not registered: {wanted}"

        required = bool(adapter.required())
        if not required:
            try:
                healthy = bool(adapter.health())
            except Exception:
                healthy = False
            if not healthy:
                self._set_state(wanted, "idle", False, "Not required")
                return True, "Idle"
            self._set_state(wanted, "stopping", True, "Stopping")
            try:
                ok, message = _normalize_action_result(
                    adapter.stop(), success_message=f"{wanted} stopped"
                )
            except Exception as exc:
                self._set_state(wanted, "error", True, str(exc))
                return False, str(exc)
            if not ok:
                self._set_state(wanted, "error", True, message)
                return False, message
            if not self._wait_for_health(adapter, False, adapter.stop_timeout):
                message = f"{wanted} did not stop before timeout"
                self._set_state(wanted, "error", True, message)
                return False, message
            self._set_state(wanted, "idle", False, message)
            return True, message

        # Start/ensure is intentionally called even when health is already true.
        # Idempotent adapters may need to reconcile configuration (notably the
        # shared Remote runtime when its Pads/VFX surface set changes).
        self._set_state(wanted, "starting", False, "Starting")
        try:
            ok, message = _normalize_action_result(
                adapter.start(), success_message=f"{wanted} started"
            )
        except Exception as exc:
            self._set_state(wanted, "error", False, str(exc))
            return False, str(exc)
        if not ok:
            self._set_state(wanted, "error", False, message)
            return False, message
        if not self._wait_for_health(adapter, True, adapter.start_timeout):
            message = f"{wanted} did not become healthy before timeout"
            self._set_state(wanted, "error", False, message)
            return False, message
        self._set_state(wanted, "running", True, message)
        return True, message

    def reconcile_module(self, module_id: str) -> tuple[bool, str]:
        wanted = str(module_id or "").strip().lower()
        runtime_ids = list(self.runtimes_for_module(wanted))
        # Pads/VFX/Remote alter the one shared Remote runtime even though Pads
        # and Voice FX do not depend on remote.server as a hard runtime.
        if wanted in {"pads", "voice_fx", "remote"} and "remote.server" in self._adapters:
            runtime_ids.append("remote.server")
        runtime_ids = list(dict.fromkeys(runtime_ids))
        if not runtime_ids:
            return True, "No dedicated runtime; hot state applied"
        messages: list[str] = []
        for runtime_id in runtime_ids:
            ok, message = self.reconcile(runtime_id)
            messages.append(f"{runtime_id}: {message}")
            if not ok:
                return False, "; ".join(messages)
        return True, "; ".join(messages)

    def reconcile_all(self) -> tuple[bool, str]:
        messages: list[str] = []
        for runtime_id in self.runtime_ids():
            ok, message = self.reconcile(runtime_id)
            messages.append(f"{runtime_id}: {message}")
            if not ok:
                return False, "; ".join(messages)
        return True, "; ".join(messages)

    def restart(self, runtime_id: str) -> tuple[bool, str]:
        wanted = self._key(runtime_id)
        adapter = self._adapters.get(wanted)
        if adapter is None:
            return False, f"Runtime not registered: {wanted}"
        if not adapter.required():
            return self.reconcile(wanted)

        self._set_state(wanted, "stopping", False, "Restarting")
        try:
            ok, message = _normalize_action_result(
                adapter.stop(), success_message=f"{wanted} stopped"
            )
        except Exception as exc:
            self._set_state(wanted, "error", False, str(exc))
            return False, str(exc)
        if not ok:
            self._set_state(wanted, "error", False, message)
            return False, message
        if not self._wait_for_health(adapter, False, adapter.stop_timeout):
            message = f"{wanted} did not stop before restart timeout"
            self._set_state(wanted, "error", True, message)
            return False, message
        return self.reconcile(wanted)

    def snapshot(self) -> dict:
        return {
            "runtimes": [
                {
                    "id": status.runtime_id,
                    "required": status.required,
                    "state": status.state,
                    "healthy": status.healthy,
                    "detail": status.detail,
                }
                for status in (self.status(runtime_id) for runtime_id in self.runtime_ids())
            ]
        }
