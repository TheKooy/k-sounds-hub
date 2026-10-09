from __future__ import annotations

import json
import zipfile

from types import SimpleNamespace

import pytest

from ksound_hub.module_registry import (
    ModuleRegistry,
    DEFAULT_MODULE_IDS,
)
from ksound_hub.module_manager import ModuleManager
from ksound_hub.module_packages import (
    LocalModulePackageInstaller,
)


def _installer(tmp_path):
    settings = SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(DEFAULT_MODULE_IDS),
    )

    registry = ModuleRegistry(
        settings,
        external_root=tmp_path / "installed",
    )

    installer = LocalModulePackageInstaller(
        ModuleManager(registry)
    )

    return registry, installer


def _write(path, depends=(), min_versions=None):
    manifest = {
        "id": "melodies_demo",
        "name": "Melodies Demo",
        "version": "1.0.0",
        "ksounds_api": 1,
        "depends": list(depends),
        "min_versions": min_versions or {},
    }

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "module.json",
            json.dumps(manifest),
        )


def test_unknown_dependency_rejected(tmp_path):
    registry, installer = _installer(tmp_path)

    package = tmp_path / "bad.ksmodule"

    _write(
        package,
        depends=["missing_library"],
    )

    with pytest.raises(
        ValueError,
        match="missing_library",
    ):
        installer.install_archive(package)

    assert not registry.is_installed(
        "melodies_demo"
    )


def test_incompatible_dependency_rejected(tmp_path):
    _, installer = _installer(tmp_path)

    package = tmp_path / "bad.ksmodule"

    _write(
        package,
        depends=["eq"],
        min_versions={"eq": "99.0.0"},
    )

    with pytest.raises(
        ValueError,
        match="eq>=99.0.0",
    ):
        installer.install_archive(package)


def test_duplicate_archive_path_rejected(tmp_path):
    _, installer = _installer(tmp_path)

    package = tmp_path / "bad.ksmodule"

    _write(package)

    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr(
            "module.json",
            "{}",
        )

    with pytest.raises(
        ValueError,
        match="duplicate",
    ):
        installer.inspect_archive(package)
