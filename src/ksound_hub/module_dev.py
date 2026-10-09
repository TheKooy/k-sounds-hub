from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .module_registry import MODULE_API_VERSION, ModuleManifest

WORK_CONTRACT_SCHEMA = 1


def _normalise_repo_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


def _path_matches_rule(path: str, rule: str) -> bool:
    wanted = _normalise_repo_path(path)
    declared = _normalise_repo_path(rule)
    if declared.endswith("/**"):
        prefix = declared[:-3].rstrip("/")
        return wanted == prefix or wanted.startswith(prefix + "/")
    if declared.endswith("/"):
        prefix = declared.rstrip("/")
        return wanted == prefix or wanted.startswith(prefix + "/")
    return wanted == declared


@dataclass(frozen=True)
class ModulePathClassification:
    owned: tuple[str, ...]
    shared: tuple[str, ...]
    foreign: tuple[str, ...]

    @property
    def parallel_safe(self) -> bool:
        return not self.shared and not self.foreign


@dataclass(frozen=True)
class ModuleWorkContract:
    schema: int
    module_id: str
    module_version: str
    ksounds_api: int
    core_build: str
    owned_hashes: dict[str, str | None]
    shared_hashes: dict[str, str | None]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ModuleWorkContract":
        if int(data.get("schema", 0)) != WORK_CONTRACT_SCHEMA:
            raise ValueError("unsupported module work contract schema")
        return cls(
            schema=WORK_CONTRACT_SCHEMA,
            module_id=str(data.get("module_id") or "").strip().lower(),
            module_version=str(data.get("module_version") or "").strip(),
            ksounds_api=int(data.get("ksounds_api", 0)),
            core_build=str(data.get("core_build") or "").strip(),
            owned_hashes={str(k): (None if v is None else str(v)) for k, v in dict(data.get("owned_hashes") or {}).items()},
            shared_hashes={str(k): (None if v is None else str(v)) for k, v in dict(data.get("shared_hashes") or {}).items()},
        )


def classify_touched_paths(manifest: ModuleManifest, paths: Iterable[object]) -> ModulePathClassification:
    owned: list[str] = []
    shared: list[str] = []
    foreign: list[str] = []
    owned_rules = tuple(manifest.owned_paths)
    shared_rules = tuple(manifest.shared_paths)

    for item in paths:
        path = _normalise_repo_path(item)
        if any(_path_matches_rule(path, rule) for rule in owned_rules):
            owned.append(path)
        elif any(_path_matches_rule(path, rule) for rule in shared_rules):
            shared.append(path)
        else:
            foreign.append(path)

    return ModulePathClassification(tuple(owned), tuple(shared), tuple(foreign))


def _sha256_or_missing(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_rules(repo_root: Path, rules: Iterable[str]) -> dict[str, str | None]:
    root = Path(repo_root).resolve()
    result: dict[str, str | None] = {}
    for rule in rules:
        normalised = _normalise_repo_path(rule)
        if normalised.endswith("/**") or normalised.endswith("/"):
            prefix = normalised[:-3] if normalised.endswith("/**") else normalised.rstrip("/")
            directory = root / prefix
            if directory.is_dir():
                for child in sorted(path for path in directory.rglob("*") if path.is_file()):
                    rel = child.relative_to(root).as_posix()
                    result[rel] = _sha256_or_missing(child)
            else:
                result[prefix] = None
        else:
            result[normalised] = _sha256_or_missing(root / normalised)
    return result


def detect_core_build(repo_root: Path) -> str:
    glass = Path(repo_root) / "src/ksound_hub/glass_app.py"
    try:
        text = glass.read_text(encoding="utf-8")
    except Exception:
        return ""
    match = re.search(r'^APP_BUILD\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def build_work_contract(repo_root: Path, manifest: ModuleManifest) -> ModuleWorkContract:
    return ModuleWorkContract(
        schema=WORK_CONTRACT_SCHEMA,
        module_id=manifest.module_id,
        module_version=manifest.version,
        ksounds_api=manifest.ksounds_api,
        core_build=detect_core_build(repo_root),
        owned_hashes=fingerprint_rules(repo_root, manifest.owned_paths),
        shared_hashes=fingerprint_rules(repo_root, manifest.shared_paths),
    )


def verify_work_contract(
    repo_root: Path,
    manifest: ModuleManifest,
    contract: ModuleWorkContract,
    *,
    check_shared: bool = False,
) -> tuple[str, ...]:
    errors: list[str] = []
    if contract.module_id != manifest.module_id:
        errors.append(f"module id changed: {contract.module_id} -> {manifest.module_id}")
    if contract.module_version != manifest.version:
        errors.append(f"module version changed: {contract.module_version} -> {manifest.version}")
    if contract.ksounds_api != MODULE_API_VERSION or manifest.ksounds_api != MODULE_API_VERSION:
        errors.append(
            f"core API mismatch: contract={contract.ksounds_api} manifest={manifest.ksounds_api} runtime={MODULE_API_VERSION}"
        )

    current_owned = fingerprint_rules(repo_root, contract.owned_hashes.keys())
    for path, expected in contract.owned_hashes.items():
        actual = current_owned.get(path)
        if actual != expected:
            errors.append(f"owned path changed: {path}")

    if check_shared:
        current_shared = fingerprint_rules(repo_root, contract.shared_hashes.keys())
        for path, expected in contract.shared_hashes.items():
            actual = current_shared.get(path)
            if actual != expected:
                errors.append(f"shared path changed: {path}")

    return tuple(errors)


def dump_contract(contract: ModuleWorkContract) -> str:
    return json.dumps(contract.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
