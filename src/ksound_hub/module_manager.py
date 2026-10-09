from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .module_registry import (
    MODULE_API_VERSION,
    ModuleManifest,
    ModuleRegistry,
    module_version_tuple,
    normalize_module_version,
)


class ModulePhase(str, Enum):
    """User-visible lifecycle phases used by the future in-app package UI.

    IP14a2 establishes the state machine now.  Download transport and the
    per-runtime supervisor will drive the transient states in later IP14 steps.
    """

    NOT_INSTALLED = "not_installed"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    INSTALLED_DISABLED = "installed_disabled"
    STARTING = "starting"
    ENABLED = "enabled"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    ERROR = "error"
    UPDATING = "updating"
    UNINSTALLING = "uninstalling"


_BUSY_PHASES = {
    ModulePhase.DOWNLOADING,
    ModulePhase.INSTALLING,
    ModulePhase.STARTING,
    ModulePhase.STOPPING,
    ModulePhase.UPDATING,
    ModulePhase.UNINSTALLING,
}


@dataclass(frozen=True)
class ModuleCatalogEntry:
    module_id: str
    name: str
    version: str
    download_url: str
    sha256: str
    ksounds_api: int = 1
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ModuleCatalogEntry":
        module_id = str(data.get("id") or "").strip().lower()
        name = str(data.get("name") or module_id).strip() or module_id
        version = normalize_module_version(data.get("version") or "")
        download_url = str(data.get("download") or data.get("download_url") or "").strip()
        sha256 = str(data.get("sha256") or "").strip().lower()
        api = int(data.get("ksounds_api", 1))
        if not module_id or not version or not download_url:
            raise ValueError("catalog entry is missing id/version/download")
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError(f"invalid sha256 for {module_id}")
        return cls(
            module_id=module_id,
            name=name,
            version=version,
            download_url=download_url,
            sha256=sha256,
            ksounds_api=api,
            description=str(data.get("description") or "").strip(),
        )


@dataclass(frozen=True)
class ModuleStatus:
    module_id: str
    name: str
    version: str
    catalog_version: str
    update_available: bool
    core_api: int
    phase: ModulePhase
    installed: bool
    enabled: bool
    runtime_required: bool
    runtime_state: str
    removable: bool
    can_uninstall: bool
    detail: str = ""
    loaded_version: str = ""


class ModuleManager:
    """Desired-state/lifecycle facade above ModuleRegistry.

    The manager deliberately separates four questions:
      * is the package installed?
      * is the module enabled by the user/profile?
      * is a runtime currently required?
      * has that runtime actually reported healthy/running?

    That separation is what lets Remote stay installed/enabled while its server
    is completely stopped when Pads and Voice FX are both unavailable.
    """

    def __init__(self, registry: ModuleRegistry):
        self.registry = registry
        self._phase_overrides: dict[str, ModulePhase] = {}
        self._runtime_state: dict[str, str] = {}
        self._loaded_version: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._catalog: dict[str, ModuleCatalogEntry] = {}

    @staticmethod
    def _key(module_id: str) -> str:
        return str(module_id or "").strip().lower()

    def set_catalog(self, entries: Iterable[ModuleCatalogEntry]) -> None:
        catalog: dict[str, ModuleCatalogEntry] = {}
        for entry in entries:
            if not isinstance(entry, ModuleCatalogEntry):
                continue
            catalog[entry.module_id] = entry
        self._catalog = catalog

    def catalog_entries(self) -> tuple[ModuleCatalogEntry, ...]:
        return tuple(sorted(self._catalog.values(), key=lambda item: item.name.lower()))

    def catalog_entry(self, module_id: str) -> ModuleCatalogEntry | None:
        return self._catalog.get(self._key(module_id))

    def begin(self, module_id: str, phase: ModulePhase) -> bool:
        wanted = self._key(module_id)
        if phase not in _BUSY_PHASES:
            return False
        current = self._phase_overrides.get(wanted)
        if current in _BUSY_PHASES:
            return False
        self._phase_overrides[wanted] = phase
        self._errors.pop(wanted, None)
        return True

    def finish(self, module_id: str) -> None:
        wanted = self._key(module_id)
        self._phase_overrides.pop(wanted, None)
        self._errors.pop(wanted, None)

    def fail(self, module_id: str, message: str) -> None:
        wanted = self._key(module_id)
        self._phase_overrides[wanted] = ModulePhase.ERROR
        self._errors[wanted] = str(message or "Module operation failed")

    def mark_runtime_failure(self, module_id: str, message: str) -> None:
        """Record a runtime crash without changing the user's desired state.

        Installed/enabled are persisted user policy. A worker crash is runtime
        truth only: live surfaces disappear while the module remains enabled
        and can be restarted explicitly or restored on the next K-Sounds start.
        """
        wanted = self._key(module_id)
        self._runtime_state[wanted] = "error"
        self._loaded_version.pop(wanted, None)
        self._phase_overrides[wanted] = ModulePhase.ERROR
        self._errors[wanted] = str(message or "Module runtime failed")

    def mark_runtime_state(self, module_id: str, state: str) -> None:
        wanted = self._key(module_id)
        normalized = str(state or "").strip().lower()
        if normalized not in {"running", "stopped", "starting", "stopping", "error", "idle"}:
            normalized = "unknown"
        self._runtime_state[wanted] = normalized
        if normalized == "running":
            manifest = self.registry.manifest(wanted)
            if manifest is not None:
                self._loaded_version[wanted] = manifest.version
        elif normalized in {"stopped", "idle"}:
            self._loaded_version.pop(wanted, None)
        if normalized == "error":
            self._phase_overrides[wanted] = ModulePhase.ERROR
        elif self._phase_overrides.get(wanted) == ModulePhase.ERROR and normalized == "running":
            self._phase_overrides.pop(wanted, None)
        # STARTING/STOPPING are owned by the controller transaction in IP14b.
        # Runtime health updates must not clear them early, otherwise the UI can
        # accept a second toggle before the first transition has fully finished.

    def runtime_required(self, module_id: str) -> bool:
        wanted = self._key(module_id)
        if wanted == "remote":
            return self.registry.remote_runtime_required()
        manifest = self.registry.manifest(wanted)
        return bool(
            manifest is not None
            and self.registry.is_enabled(wanted)
            and (manifest.runtimes or not manifest.builtin)
        )

    def can_uninstall(self, module_id: str) -> bool:
        wanted = self._key(module_id)
        manifest = self.registry.manifest(wanted)
        if manifest is None or manifest.builtin or not manifest.removable:
            return False
        if not self.registry.is_installed(wanted) or self.registry.is_enabled(wanted):
            return False
        if self.registry.enabled_dependents(wanted):
            return False
        return self._phase_overrides.get(wanted) not in _BUSY_PHASES

    def request_enable(self, module_id: str, enabled: bool) -> tuple[bool, str]:
        wanted = self._key(module_id)
        if self._phase_overrides.get(wanted) in _BUSY_PHASES:
            return False, "Module operation already in progress"
        if not self.registry.is_installed(wanted):
            return False, "Module is not installed"
        if enabled:
            missing = self.registry.missing_dependencies(wanted)
            if missing:
                return False, "Missing dependencies: " + ", ".join(missing)
            incompatible = self.registry.incompatible_dependencies(wanted)
            if incompatible:
                return False, "Dependency version mismatch: " + ", ".join(incompatible)
        if not self.registry.set_enabled(wanted, bool(enabled)):
            dependents = self.registry.enabled_dependents(wanted)
            if dependents and not enabled:
                return False, "Required by enabled modules: " + ", ".join(dependents)
            return False, "Module state change was rejected"
        self._errors.pop(wanted, None)
        # Runtime Supervisor (IP14b) will convert enable/disable requests into
        # STARTING/STOPPING and wait for readiness.  IP14a2 keeps the desired
        # state truthful rather than pretending a runtime has already booted.
        self._runtime_state.pop(wanted, None)
        return True, "Enabled" if enabled else "Disabled"

    def status(self, module_id: str) -> ModuleStatus:
        wanted = self._key(module_id)
        manifest: ModuleManifest | None = self.registry.manifest(wanted)
        catalog = self.catalog_entry(wanted)
        if manifest is None:
            return ModuleStatus(
                module_id=wanted,
                name=catalog.name if catalog is not None else wanted,
                version="",
                loaded_version="",
                catalog_version=catalog.version if catalog is not None else "",
                update_available=False,
                core_api=(catalog.ksounds_api if catalog is not None else MODULE_API_VERSION),
                phase=ModulePhase.NOT_INSTALLED,
                installed=False,
                enabled=False,
                runtime_required=False,
                runtime_state="stopped",
                removable=False,
                can_uninstall=False,
            )

        installed = self.registry.is_installed(wanted)
        enabled = self.registry.is_enabled(wanted)
        runtime_required = self.runtime_required(wanted)
        runtime_state = self._runtime_state.get(wanted, "unconfirmed")
        loaded_version = self._loaded_version.get(wanted, "")
        override = self._phase_overrides.get(wanted)

        if override is not None:
            phase = override
        elif not installed:
            phase = ModulePhase.NOT_INSTALLED
        elif not enabled:
            phase = ModulePhase.INSTALLED_DISABLED
            runtime_state = "stopped"
            loaded_version = ""
        elif wanted == "remote" and not runtime_required:
            # Remote can remain installed + enabled while consuming exactly
            # zero runtime resources when no Remote surface is available.
            phase = ModulePhase.IDLE
            runtime_state = "idle"
            loaded_version = ""
        elif runtime_state == "running":
            phase = ModulePhase.RUNNING
        else:
            phase = ModulePhase.ENABLED

        catalog_version = catalog.version if catalog is not None else ""
        update_available = bool(
            catalog is not None
            and catalog.ksounds_api == MODULE_API_VERSION
            and module_version_tuple(catalog.version) > module_version_tuple(manifest.version)
        )
        detail = self._errors.get(wanted, "")
        return ModuleStatus(
            module_id=wanted,
            name=manifest.name,
            version=manifest.version,
            loaded_version=loaded_version,
            catalog_version=catalog_version,
            update_available=update_available,
            core_api=manifest.ksounds_api,
            phase=phase,
            installed=installed,
            enabled=enabled,
            runtime_required=runtime_required,
            runtime_state=runtime_state,
            removable=manifest.removable,
            can_uninstall=self.can_uninstall(wanted),
            detail=detail,
        )

    def snapshot(self) -> dict:
        module_ids = {item.module_id for item in self.registry.manifests()} | set(self._catalog)
        return {
            "modules": [
                {
                    "id": status.module_id,
                    "name": status.name,
                    "version": status.version,
                    "loaded_version": status.loaded_version,
                    "catalog_version": status.catalog_version,
                    "update_available": status.update_available,
                    "core_api": status.core_api,
                    "phase": status.phase.value,
                    "installed": status.installed,
                    "enabled": status.enabled,
                    "runtime_required": status.runtime_required,
                    "runtime_state": status.runtime_state,
                    "removable": status.removable,
                    "can_uninstall": status.can_uninstall,
                    "detail": status.detail,
                }
                for status in (self.status(module_id) for module_id in sorted(module_ids))
            ],
            "remote_surfaces": list(self.registry.remote_surfaces()),
            "remote_runtime_required": self.registry.remote_runtime_required(),
        }
