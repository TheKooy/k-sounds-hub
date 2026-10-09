from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MODULE_API_VERSION = 1
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
MODULE_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def normalize_module_version(value: object) -> str:
    """Normalize legacy 1 / 1.2 versions into stable MAJOR.MINOR.PATCH."""
    raw = str(value or "").strip()
    if re.fullmatch(r"\d+", raw):
        raw = f"{raw}.0.0"
    elif re.fullmatch(r"\d+\.\d+", raw):
        raw = f"{raw}.0"
    if not MODULE_VERSION_RE.fullmatch(raw):
        raise ValueError(f"invalid module version: {raw!r}")
    return raw


def module_version_tuple(value: object) -> tuple[int, int, int]:
    normalized = normalize_module_version(value)
    return tuple(int(part) for part in normalized.split("."))  # type: ignore[return-value]


def module_version_at_least(current: object, minimum: object) -> bool:
    return module_version_tuple(current) >= module_version_tuple(minimum)

# IP14f keeps bundled module code inside the application tree for now, but
# each module owns a separate manifest/version and development boundary.
# installed/enabled/runtime remain separate states.
DEFAULT_MODULE_IDS = (
    "eq",
    "micro_tune",
    "hard_gate",
    "voice_fx",
    "pads",
    "remote",
)
DEFAULT_ENABLED_MODULE_IDS = DEFAULT_MODULE_IDS


@dataclass(frozen=True)
class ModuleManifest:
    module_id: str
    name: str
    version: str
    description: str = ""
    min_versions: tuple[tuple[str, str], ...] = ()
    ksounds_api: int = MODULE_API_VERSION
    provides: tuple[str, ...] = ()
    depends: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()
    runtimes: tuple[str, ...] = ()
    nav_label: str = ""
    nav_icon: str = ""
    nav_order: int = 100
    builtin: bool = False
    removable: bool = True
    hot_reload: bool = False
    owned_paths: tuple[str, ...] = ()
    shared_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict, *, builtin: bool = False) -> "ModuleManifest":
        module_id = str(data.get("id") or "").strip().lower()
        if not MODULE_ID_RE.fullmatch(module_id):
            raise ValueError(f"invalid module id: {module_id!r}")
        api = int(data.get("ksounds_api", MODULE_API_VERSION))
        if api != MODULE_API_VERSION:
            raise ValueError(f"unsupported module API {api} for {module_id}")

        def clean_list(key: str) -> tuple[str, ...]:
            raw = data.get(key, [])
            if not isinstance(raw, list):
                return ()
            result: list[str] = []
            seen: set[str] = set()
            for item in raw:
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                result.append(value)
            return tuple(result)

        raw_min_versions = data.get("min_versions", {})
        min_versions: list[tuple[str, str]] = []
        if isinstance(raw_min_versions, dict):
            for dependency, minimum in sorted(raw_min_versions.items()):
                dep = str(dependency or "").strip().lower()
                if not MODULE_ID_RE.fullmatch(dep):
                    raise ValueError(f"invalid dependency id in min_versions: {dep!r}")
                min_versions.append((dep, normalize_module_version(minimum)))

        return cls(
            module_id=module_id,
            name=str(data.get("name") or module_id).strip() or module_id,
            version=normalize_module_version(data.get("version") or "1.0.0"),
            description=str(data.get("description") or "").strip(),
            owned_paths=clean_list("owned_paths"),
            shared_paths=clean_list("shared_paths"),
            ksounds_api=api,
            min_versions=tuple(min_versions),
            provides=clean_list("provides"),
            depends=clean_list("depends"),
            optional_dependencies=clean_list("optional_dependencies"),
            runtimes=clean_list("runtimes"),
            nav_label=str(data.get("nav_label") or "").strip(),
            nav_icon=str(data.get("nav_icon") or "").strip(),
            nav_order=int(data.get("nav_order", 100)),
            builtin=bool(builtin),
            removable=(False if builtin else bool(data.get("removable", True))),
            hot_reload=bool(data.get("hot_reload", False)),
        )


BUILTIN_MANIFEST_ROOT = Path(__file__).resolve().parent / "builtin_modules"


def _load_builtin_manifests() -> dict[str, ModuleManifest]:
    manifests: dict[str, ModuleManifest] = {}
    for module_id in DEFAULT_MODULE_IDS:
        path = BUILTIN_MANIFEST_ROOT / module_id / "module.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid bundled module manifest: {path}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"bundled module manifest must be an object: {path}")
        manifest = ModuleManifest.from_dict(raw, builtin=True)
        if manifest.module_id != module_id:
            raise RuntimeError(
                f"bundled module id mismatch: expected {module_id}, got {manifest.module_id}"
            )
        manifests[module_id] = manifest
    return manifests


BUILTIN_MANIFESTS = _load_builtin_manifests()


class ModuleRegistry:
    """Validated module/capability registry.

    IP14a3 does not execute third-party Python.  External module directories
    only contribute a validated manifest and payload files.  This is
    intentional: package download/verification and runtime loading remain two
    separate trust boundaries.
    """

    def __init__(self, settings, *, external_root: Path | None = None):
        self.settings = settings
        self.external_root = (
            external_root
            if external_root is not None
            else Path.home() / ".local" / "share" / "k-sounds-hub" / "modules"
        )
        self._manifests: dict[str, ModuleManifest] = {}
        self.reload_external_manifests()

    def reload_external_manifests(self) -> None:
        self._manifests = _load_builtin_manifests()
        root = self.external_root
        if root.is_dir():
            for path in sorted(root.glob("*/module.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        continue
                    manifest = ModuleManifest.from_dict(raw, builtin=False)
                except Exception:
                    continue
                # Bundled IDs cannot be shadowed by a downloaded module.
                if manifest.module_id in BUILTIN_MANIFESTS:
                    continue
                # The directory name is part of the package identity.  A
                # mismatched manifest is ignored rather than silently moved.
                if path.parent.name != manifest.module_id:
                    continue
                self._manifests[manifest.module_id] = manifest
        self._normalize_settings()

    @staticmethod
    def _clean_ids(values: Iterable[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            value = str(item or "").strip().lower()
            if not value or not MODULE_ID_RE.fullmatch(value) or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _normalize_settings(self) -> None:
        installed = self._clean_ids(getattr(self.settings, "installed_modules", []))
        enabled = self._clean_ids(getattr(self.settings, "enabled_modules", []))
        pre_ip14k_without_hard_gate = bool(installed) and "hard_gate" not in installed

        # Existing IP13 users migrate with all bundled modules installed and
        # enabled.  Bundled files physically live in the application tree in
        # IP14a3, so they cannot truthfully become NOT_INSTALLED yet.
        if not installed:
            installed = list(DEFAULT_MODULE_IDS)

        known = set(self._manifests)
        installed = [module_id for module_id in installed if module_id in known]
        for module_id in DEFAULT_MODULE_IDS:
            if module_id not in installed:
                installed.append(module_id)

        enabled = [module_id for module_id in enabled if module_id in installed]
        # Hard Gate had no independent module state before IP14k. Preserve the
        # previous effective behavior once, but never re-enable it after an
        # IP14k+ user deliberately disables the module.
        if pre_ip14k_without_hard_gate and "hard_gate" in installed and "hard_gate" not in enabled:
            enabled.append("hard_gate")
        self.settings.installed_modules = installed
        self.settings.enabled_modules = enabled

    def manifests(self) -> tuple[ModuleManifest, ...]:
        return tuple(
            sorted(
                self._manifests.values(),
                key=lambda item: (item.nav_order, item.name.lower()),
            )
        )

    def manifest(self, module_id: str) -> ModuleManifest | None:
        return self._manifests.get(str(module_id or "").strip().lower())

    def is_installed(self, module_id: str) -> bool:
        wanted = str(module_id or "").strip().lower()
        return (
            wanted in set(getattr(self.settings, "installed_modules", []))
            and wanted in self._manifests
        )

    def is_enabled(self, module_id: str) -> bool:
        wanted = str(module_id or "").strip().lower()
        return self.is_installed(wanted) and wanted in set(
            getattr(self.settings, "enabled_modules", [])
        )

    def missing_dependencies(self, module_id: str) -> tuple[str, ...]:
        manifest = self.manifest(module_id)
        if manifest is None:
            return ()
        missing: list[str] = []
        for dependency in manifest.depends:
            dep = str(dependency or "").strip().lower()
            # Capabilities/core APIs may be introduced as dependency tokens in
            # later manifests.  IP14a3 only resolves concrete module IDs.
            if not self.is_installed(dep):
                missing.append(dep)
        return tuple(missing)

    def incompatible_dependencies(self, module_id: str) -> tuple[str, ...]:
        manifest = self.manifest(module_id)
        if manifest is None:
            return ()
        minimums = dict(manifest.min_versions)
        incompatible: list[str] = []
        for dependency in manifest.depends:
            dep = str(dependency or "").strip().lower()
            minimum = minimums.get(dep)
            if not minimum or not self.is_installed(dep):
                continue
            installed = self.manifest(dep)
            if installed is not None and not module_version_at_least(installed.version, minimum):
                incompatible.append(f"{dep}>={minimum}")
        return tuple(incompatible)

    def enabled_dependents(self, module_id: str) -> tuple[str, ...]:
        wanted = str(module_id or "").strip().lower()
        result: list[str] = []
        for manifest in self.manifests():
            if wanted in manifest.depends and self.is_enabled(manifest.module_id):
                result.append(manifest.module_id)
        return tuple(result)

    def set_installed(self, module_id: str, installed: bool) -> bool:
        wanted = str(module_id or "").strip().lower()
        manifest = self.manifest(wanted)
        if manifest is None:
            return False
        if manifest.builtin and not installed:
            return False

        current = self._clean_ids(getattr(self.settings, "installed_modules", []))
        enabled = self._clean_ids(getattr(self.settings, "enabled_modules", []))
        if installed:
            if wanted not in current:
                current.append(wanted)
        else:
            if self.enabled_dependents(wanted):
                return False
            current = [item for item in current if item != wanted]
            enabled = [item for item in enabled if item != wanted]
        self.settings.installed_modules = current
        self.settings.enabled_modules = [item for item in enabled if item in current]
        return True

    def set_enabled(self, module_id: str, enabled: bool) -> bool:
        wanted = str(module_id or "").strip().lower()
        if not self.is_installed(wanted):
            return False
        if enabled and self.missing_dependencies(wanted):
            return False
        if enabled and self.incompatible_dependencies(wanted):
            return False

        current = self._clean_ids(getattr(self.settings, "enabled_modules", []))
        if enabled:
            if wanted not in current:
                current.append(wanted)
        else:
            # A required dependency cannot be disabled underneath an enabled
            # consumer.  Optional dependencies (Remote for Pads/VFX) do not
            # participate in this rule.
            if self.enabled_dependents(wanted):
                return False
            current = [item for item in current if item != wanted]
        self.settings.enabled_modules = current
        return True

    def provides(self, capability: str, *, enabled_only: bool = True) -> tuple[str, ...]:
        wanted = str(capability or "").strip()
        providers: list[str] = []
        for module_id, manifest in self._manifests.items():
            if wanted not in manifest.provides:
                continue
            if enabled_only and not self.is_enabled(module_id):
                continue
            if not enabled_only and not self.is_installed(module_id):
                continue
            providers.append(module_id)
        return tuple(providers)

    def remote_surfaces(self) -> tuple[str, ...]:
        """Surfaces served by the one shared Remote runtime."""
        if not self.is_enabled("remote"):
            return ()
        result: list[str] = []
        if self.is_enabled("pads") and self.provides("remote.pads"):
            result.append("pads")
        if self.is_enabled("voice_fx") and self.provides("remote.vfx"):
            result.append("voice_fx")
        return tuple(result)

    def remote_runtime_required(self) -> bool:
        return bool(self.remote_surfaces())

    def navigation_modules(self) -> tuple[ModuleManifest, ...]:
        return tuple(
            manifest
            for manifest in self.manifests()
            if self.is_enabled(manifest.module_id)
            and manifest.nav_label
            and manifest.nav_icon
        )

    def snapshot(self) -> dict:
        return {
            "api": MODULE_API_VERSION,
            "modules": [
                {
                    "id": manifest.module_id,
                    "name": manifest.name,
                    "version": manifest.version,
                    "ksounds_api": manifest.ksounds_api,
                    "owned_paths": list(manifest.owned_paths),
                    "shared_paths": list(manifest.shared_paths),
                    "min_versions": dict(manifest.min_versions),
                    "builtin": manifest.builtin,
                    "removable": manifest.removable,
                    "installed": self.is_installed(manifest.module_id),
                    "enabled": self.is_enabled(manifest.module_id),
                    "provides": list(manifest.provides),
                    "depends": list(manifest.depends),
                    "runtimes": list(manifest.runtimes),
                    "hot_reload": manifest.hot_reload,
                }
                for manifest in self.manifests()
            ],
            "remote_surfaces": list(self.remote_surfaces()),
            "remote_runtime_required": self.remote_runtime_required(),
        }
