from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .module_manager import ModuleCatalogEntry, ModuleManager, ModulePhase
from .module_registry import BUILTIN_MANIFESTS, ModuleManifest, module_version_tuple

KSMODULE_MAX_FILES = 2048
KSMODULE_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
CATALOG_SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_catalog(data: dict) -> tuple[ModuleCatalogEntry, ...]:
    if not isinstance(data, dict):
        raise ValueError("module catalog must be an object")
    schema = int(data.get("schema", CATALOG_SCHEMA_VERSION))
    if schema != CATALOG_SCHEMA_VERSION:
        raise ValueError(f"unsupported module catalog schema: {schema}")
    raw_modules = data.get("modules", [])
    if isinstance(raw_modules, dict):
        values = []
        for module_id, raw in raw_modules.items():
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item.setdefault("id", str(module_id))
            values.append(item)
        raw_modules = values
    if not isinstance(raw_modules, list):
        raise ValueError("catalog modules must be a list or object")
    entries = [ModuleCatalogEntry.from_dict(item) for item in raw_modules if isinstance(item, dict)]
    seen: set[str] = set()
    result: list[ModuleCatalogEntry] = []
    for entry in entries:
        if entry.module_id in seen:
            raise ValueError(f"duplicate module in catalog: {entry.module_id}")
        seen.add(entry.module_id)
        result.append(entry)
    return tuple(result)


def load_catalog_file(path: Path) -> tuple[ModuleCatalogEntry, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return parse_catalog(payload)


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = str(info.filename or "")
    if not name or "\\" in name:
        raise ValueError(f"invalid archive path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe archive path: {name}")
    # Unix symlinks can escape the destination even with a safe textual path.
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise ValueError(f"symlink is not allowed in module archive: {name}")


class LocalModulePackageInstaller:
    """Atomic local .ksmodule installer used by the future Git transport.

    A .ksmodule is a ZIP file with module.json at its root.  IP14a3 installs
    and validates payload files but deliberately does not execute third-party
    module code.  Later the Git catalog downloader can hand its verified file
    directly to this class without changing installation semantics.
    """

    def __init__(self, manager: ModuleManager):
        self.manager = manager
        self.registry = manager.registry

    def inspect_archive(self, archive: Path, *, expected_sha256: str = "") -> ModuleManifest:
        path = Path(archive).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if expected_sha256:
            expected = str(expected_sha256).strip().lower()
            actual = sha256_file(path)
            if actual != expected:
                raise ValueError(f"module checksum mismatch: expected {expected}, got {actual}")

        with zipfile.ZipFile(path, "r") as package:
            infos = package.infolist()
            if len(infos) > KSMODULE_MAX_FILES:
                raise ValueError("module archive contains too many files")
            total = 0
            names: set[str] = set()
            for info in infos:
                _validate_zip_member(info)
                total += max(0, int(info.file_size))
                if total > KSMODULE_MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("module archive is too large after extraction")
                member_name = info.filename.rstrip("/")
                if member_name in names:
                    raise ValueError(
                        f"duplicate module archive path: {member_name}"
                    )
                names.add(member_name)
            if "module.json" not in names:
                raise ValueError("module archive has no root module.json")
            raw = json.loads(package.read("module.json").decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("module.json must be an object")
            manifest = ModuleManifest.from_dict(raw, builtin=False)
            from .module_extensions import validate_extensions
            validate_extensions(raw)  # reject invalid contributions before writing files
            if manifest.module_id in BUILTIN_MANIFESTS:
                raise ValueError(f"downloaded module cannot replace bundled id {manifest.module_id}")
            return manifest

    def install_archive(self, archive: Path, *, expected_sha256: str = "") -> ModuleManifest:
        manifest = self.inspect_archive(archive, expected_sha256=expected_sha256)
        module_id = manifest.module_id
        if self.registry.is_installed(module_id):
            raise ValueError(f"module is already installed: {module_id}")
        missing = [
            dep for dep in manifest.depends
            if not self.registry.is_installed(dep)
        ]
        if missing:
            raise ValueError("missing dependencies: " + ", ".join(missing))
        incompatible = [
            f"{dep}>={minimum}"
            for dep, minimum in manifest.min_versions
            if dep in manifest.depends
            and self.registry.is_installed(dep)
            and module_version_tuple(
                self.registry.manifest(dep).version
            ) < module_version_tuple(minimum)
        ]
        if incompatible:
            raise ValueError(
                "incompatible dependencies: "
                + ", ".join(incompatible)
            )
        if not self.manager.begin(module_id, ModulePhase.INSTALLING):
            raise RuntimeError(f"module is busy: {module_id}")

        target = self.registry.external_root / module_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.manager.fail(module_id, "Module directory already exists")
            raise ValueError(f"module directory already exists: {target}")

        staging_parent = target.parent
        staging = Path(tempfile.mkdtemp(prefix=f".{module_id}.install-", dir=staging_parent))
        try:
            with zipfile.ZipFile(Path(archive).expanduser().resolve(), "r") as package:
                for info in package.infolist():
                    _validate_zip_member(info)
                    package.extract(info, staging)
            # Parse again from the extracted tree so the installed identity is
            # exactly what ModuleRegistry will later discover.
            raw = json.loads((staging / "module.json").read_text(encoding="utf-8"))
            extracted = ModuleManifest.from_dict(raw, builtin=False)
            if extracted.module_id != module_id:
                raise ValueError("module identity changed during extraction")
            staging.rename(target)
            self.registry.reload_external_manifests()
            if self.registry.manifest(module_id) is None:
                raise ValueError("installed module manifest was not discoverable")
            if not self.registry.set_installed(module_id, True):
                raise ValueError("could not mark module installed")
            # Install is intentionally safe-by-default: newly downloaded
            # modules remain disabled until the user explicitly enables them.
            self.registry.set_enabled(module_id, False)
            self.manager.finish(module_id)
            return self.registry.manifest(module_id) or extracted
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if target.exists() and not self.registry.is_installed(module_id):
                shutil.rmtree(target, ignore_errors=True)
            self.registry.reload_external_manifests()
            self.manager.fail(module_id, str(exc))
            raise


    def update_archive(self, archive: Path, *, expected_sha256: str = "") -> ModuleManifest:
        """Atomically replace one disabled external module with a newer version."""
        manifest = self.inspect_archive(archive, expected_sha256=expected_sha256)
        module_id = manifest.module_id
        current = self.registry.manifest(module_id)
        if current is None or not self.registry.is_installed(module_id):
            raise ValueError(f"module is not installed: {module_id}")
        if current.builtin:
            raise ValueError(f"bundled module cannot be updated from .ksmodule yet: {module_id}")
        if self.registry.is_enabled(module_id):
            raise ValueError(f"disable module before update: {module_id}")
        if self.registry.enabled_dependents(module_id):
            raise ValueError("module is required by enabled dependents")
        missing = [
            dep for dep in manifest.depends
            if not self.registry.is_installed(dep)
        ]
        if missing:
            raise ValueError(
                "missing dependencies: " + ", ".join(missing)
            )
        incompatible = [
            f"{dep}>={minimum}"
            for dep, minimum in manifest.min_versions
            if dep in manifest.depends
            and self.registry.is_installed(dep)
            and module_version_tuple(
                self.registry.manifest(dep).version
            ) < module_version_tuple(minimum)
        ]
        if incompatible:
            raise ValueError(
                "incompatible dependencies: "
                + ", ".join(incompatible)
            )
        if module_version_tuple(manifest.version) <= module_version_tuple(current.version):
            raise ValueError(
                f"module update must be newer than {current.version}: got {manifest.version}"
            )
        if not self.manager.begin(module_id, ModulePhase.UPDATING):
            raise RuntimeError(f"module is busy: {module_id}")

        target = self.registry.external_root / module_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            self.manager.fail(module_id, "Installed module directory is missing")
            raise ValueError(f"module directory is missing: {target}")

        staging = Path(tempfile.mkdtemp(prefix=f".{module_id}.update-", dir=target.parent))
        backup = target.with_name(f".{module_id}.previous")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        swapped = False
        try:
            with zipfile.ZipFile(Path(archive).expanduser().resolve(), "r") as package:
                for info in package.infolist():
                    _validate_zip_member(info)
                    package.extract(info, staging)
            raw = json.loads((staging / "module.json").read_text(encoding="utf-8"))
            extracted = ModuleManifest.from_dict(raw, builtin=False)
            if extracted.module_id != module_id or extracted.version != manifest.version:
                raise ValueError("module identity/version changed during extraction")

            target.rename(backup)
            staging.rename(target)
            swapped = True
            self.registry.reload_external_manifests()
            installed = self.registry.manifest(module_id)
            if installed is None or installed.version != manifest.version:
                raise ValueError("updated module manifest was not discoverable")
            shutil.rmtree(backup, ignore_errors=True)
            self.manager.finish(module_id)
            return installed
        except Exception as exc:
            if swapped and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup.exists() and not target.exists():
                backup.rename(target)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            self.registry.reload_external_manifests()
            self.manager.fail(module_id, str(exc))
            raise

    def uninstall(self, module_id: str) -> bool:
        wanted = str(module_id or "").strip().lower()
        if not self.manager.can_uninstall(wanted):
            return False
        manifest = self.registry.manifest(wanted)
        if manifest is None or manifest.builtin:
            return False
        target = self.registry.external_root / wanted
        if not target.is_dir():
            return False
        if not self.manager.begin(wanted, ModulePhase.UNINSTALLING):
            return False

        # Move first so a failed delete can be rolled back atomically.
        tombstone = target.with_name(f".{wanted}.uninstalling")
        if tombstone.exists():
            shutil.rmtree(tombstone, ignore_errors=True)
        try:
            target.rename(tombstone)
            if not self.registry.set_installed(wanted, False):
                tombstone.rename(target)
                self.manager.finish(wanted)
                return False
            shutil.rmtree(tombstone)
            self.registry.reload_external_manifests()
            self.manager.finish(wanted)
            return True
        except Exception as exc:
            if tombstone.exists() and not target.exists():
                tombstone.rename(target)
            self.registry.reload_external_manifests()
            self.manager.fail(wanted, str(exc))
            return False
