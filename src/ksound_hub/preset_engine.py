from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR
from .dsp_registry import DspModuleRegistry


PRESET_FORMAT = "ksounds-preset"
PRESET_FORMAT_VERSION = 1
MAX_MODULES = 16


class PresetValidationError(ValueError):
    pass


def _safe_id(value: str, fallback: str = "preset") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-._")
    return (slug or fallback)[:80]


class PresetEngine:
    """Portable, non-executable K-Sounds preset storage and validation."""

    def __init__(self, registry: DspModuleRegistry, directory: Path | None = None):
        self.registry = registry
        self.directory = Path(directory or (CONFIG_DIR / "presets"))

    def new_preset(self, name: str = "Custom Lab", target: str = "vfx") -> dict[str, Any]:
        return {
            "format": PRESET_FORMAT,
            "format_version": PRESET_FORMAT_VERSION,
            "id": _safe_id(name, "custom-lab"),
            "name": str(name or "Custom Lab").strip() or "Custom Lab",
            "kind": "dsp_chain",
            "targets": [str(target or "vfx").strip().lower()],
            "wet_percent": 100.0,
            "modules": [],
            "metadata": {"portable": True},
        }

    def normalize(self, preset: dict[str, Any], *, target: str | None = None, runtime: bool = False) -> dict[str, Any]:
        if not isinstance(preset, dict):
            raise PresetValidationError("Preset must be a JSON object")
        if str(preset.get("format", PRESET_FORMAT)) != PRESET_FORMAT:
            raise PresetValidationError("Unsupported preset format")
        try:
            version = int(preset.get("format_version", PRESET_FORMAT_VERSION))
        except (TypeError, ValueError):
            version = 0
        if version != PRESET_FORMAT_VERSION:
            raise PresetValidationError(f"Unsupported preset format version: {version}")

        name = str(preset.get("name", "Custom Lab") or "Custom Lab").strip()[:96] or "Custom Lab"
        preset_id = _safe_id(str(preset.get("id", name)), _safe_id(name, "preset"))

        raw_targets = preset.get("targets", [])
        if not isinstance(raw_targets, list):
            raw_targets = []
        targets = []
        for item in raw_targets:
            normalized = str(item or "").strip().lower()
            if normalized in {"input", "output", "vfx"} and normalized not in targets:
                targets.append(normalized)
        if not targets:
            targets = [str(target or "vfx").strip().lower()]
        if target is not None:
            wanted_target = str(target or "").strip().lower()
            if wanted_target not in targets:
                raise PresetValidationError(f"Preset is not compatible with target: {wanted_target}")
        else:
            wanted_target = targets[0]

        raw_modules = preset.get("modules", [])
        if not isinstance(raw_modules, list):
            raise PresetValidationError("Preset modules must be a list")
        if len(raw_modules) > MAX_MODULES:
            raise PresetValidationError(f"Preset exceeds the {MAX_MODULES}-module safety limit")

        counts: dict[str, int] = {}
        exclusive_groups: set[str] = set()
        modules: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_modules):
            if not isinstance(raw, dict):
                raise PresetValidationError(f"Module #{index + 1} must be an object")
            module_type = str(raw.get("type", "") or "").strip().lower()
            spec = self.registry.get(module_type)
            if spec is None:
                raise PresetValidationError(f"Unknown DSP module: {module_type or '<empty>'}")
            allowed_targets = spec.runtime_targets if runtime else spec.allowed_targets
            if wanted_target not in allowed_targets:
                mode = "runtime" if runtime else "preset"
                raise PresetValidationError(f"{spec.name} is not available for {wanted_target} in {mode} mode")

            count = counts.get(module_type, 0) + 1
            counts[module_type] = count
            if count > max(1, int(spec.max_instances)):
                raise PresetValidationError(f"Too many {spec.name} modules (max {spec.max_instances})")
            if spec.exclusive_group:
                if spec.exclusive_group in exclusive_groups:
                    raise PresetValidationError(f"Conflicting module group: {spec.exclusive_group}")
                exclusive_groups.add(spec.exclusive_group)

            raw_params = raw.get("params", {})
            if not isinstance(raw_params, dict):
                raw_params = {}
            params = {param.key: param.normalize(raw_params.get(param.key, param.default)) for param in spec.parameters}
            modules.append(
                {
                    "id": _safe_id(str(raw.get("id", f"{module_type}-{index + 1}")), f"{module_type}-{index + 1}"),
                    "type": module_type,
                    "enabled": bool(raw.get("enabled", True)),
                    "params": params,
                }
            )

        try:
            wet = float(preset.get("wet_percent", 100.0))
        except (TypeError, ValueError):
            wet = 100.0
        wet = max(0.0, min(100.0, wet))

        metadata = preset.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        clean_metadata = {
            "portable": bool(metadata.get("portable", True)),
        }
        author = str(metadata.get("author", "") or "").strip()[:96]
        if author:
            clean_metadata["author"] = author

        return {
            "format": PRESET_FORMAT,
            "format_version": PRESET_FORMAT_VERSION,
            "id": preset_id,
            "name": name,
            "kind": "dsp_chain",
            "targets": targets,
            "wet_percent": wet,
            "modules": modules,
            "metadata": clean_metadata,
        }

    def runtime_chain(self, preset: dict[str, Any], target: str) -> dict[str, Any]:
        return self.normalize(preset, target=target, runtime=True)

    def save(self, preset: dict[str, Any]) -> Path:
        normalized = self.normalize(preset)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{normalized['id']}.kspreset"
        tmp = path.with_suffix(".kspreset.tmp")
        tmp.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, path: Path | str, *, target: str | None = None, runtime: bool = False) -> dict[str, Any]:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        return self.normalize(raw, target=target, runtime=runtime)

    def list_presets(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.directory.is_dir():
            return []
        items: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.directory.glob("*.kspreset")):
            try:
                items.append((path, self.load(path)))
            except Exception:
                continue
        return items

    def list_for_target(self, target: str, *, runtime: bool = False) -> list[tuple[Path, dict[str, Any]]]:
        """Return saved presets compatible with a target, skipping invalid/incompatible files."""
        items: list[tuple[Path, dict[str, Any]]] = []
        for path, preset in self.list_presets():
            try:
                normalized = self.normalize(preset, target=target, runtime=runtime)
            except PresetValidationError:
                continue
            items.append((path, normalized))
        return items

    def import_file(self, source: Path | str) -> tuple[Path, dict[str, Any]]:
        normalized = self.load(source)
        path = self.save(normalized)
        return path, normalized

    def export_file(self, preset: dict[str, Any], destination: Path | str) -> Path:
        normalized = self.normalize(preset)
        target = Path(destination)
        if target.suffix.lower() != ".kspreset":
            target = target.with_suffix(".kspreset")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    @staticmethod
    def clone(preset: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(preset)
