"""K-Sounds extension contract v1, declarative surfaces only.

A module owns its logic.  This registry never imports third-party Python or
starts external processes.  The host remains in charge of module lifecycle.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_KINDS = ("desktop", "remote", "settings")
_CONTROL_KINDS = ("label", "button", "toggle", "slider", "select")
_MAX_SURFACES = 12
_MAX_CONTROLS = 48


def _text(value: Any, label: str, *, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value.strip()


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


@dataclass(frozen=True)
class ExtensionControl:
    control_id: str
    kind: str
    label: str
    action: str = ""
    minimum: int = 0
    maximum: int = 100
    initial: int | bool | str | None = None
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ExtensionSurface:
    module_id: str
    surface_id: str
    kind: str
    label: str
    icon: str
    controls: tuple[ExtensionControl, ...]

    @property
    def route(self) -> str:
        return f"ext:{self.module_id}:{self.surface_id}"


def validate_extensions(raw_manifest: dict) -> tuple[ExtensionSurface, ...]:
    """Validate module.json extensions, or return no surfaces when omitted.

    All three surface families share identifiers within a module.  Unknown
    keys are ignored for forwards-compatible metadata, unknown types fail.
    """
    if not isinstance(raw_manifest, dict):
        raise ValueError("module manifest must be an object")
    module_id = _id(raw_manifest.get("id"), "module id")
    block = raw_manifest.get("extensions")
    if block is None:
        return ()
    if not isinstance(block, dict) or type(block.get("schema")) is not int or block["schema"] != 1:
        raise ValueError("unsupported extensions schema (expected 1)")
    result: list[ExtensionSurface] = []
    used_routes: set[str] = set()
    for kind in _KINDS:
        entries = block.get(kind, [])
        if not isinstance(entries, list) or len(entries) > _MAX_SURFACES:
            raise ValueError(f"invalid extensions.{kind} list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"invalid {kind} surface")
            sid = _id(entry.get("id"), "surface id")
            if sid in used_routes:
                raise ValueError(f"duplicate surface id: {sid}")
            used_routes.add(sid)
            label = _text(entry.get("label"), "surface label")
            icon = entry.get("icon", "◇")
            if not isinstance(icon, str) or not icon or len(icon) > 8 or any(ord(c) < 32 for c in icon):
                raise ValueError("invalid surface icon")
            controls = entry.get("controls", [])
            if not isinstance(controls, list) or len(controls) > _MAX_CONTROLS:
                raise ValueError("invalid surface controls")
            validated: list[ExtensionControl] = []
            seen: set[str] = set()
            for control in controls:
                if not isinstance(control, dict):
                    raise ValueError("invalid control")
                cid = _id(control.get("id"), "control id")
                if cid in seen:
                    raise ValueError(f"duplicate control id: {cid}")
                seen.add(cid)
                ctype = control.get("type")
                if ctype not in _CONTROL_KINDS:
                    raise ValueError(f"unsupported control type: {ctype!r}")
                clabel = _text(control.get("label"), "control label")
                action = control.get("action", "")
                if ctype != "label":
                    if not isinstance(action, str) or not _ACTION.fullmatch(action):
                        raise ValueError(f"invalid action for {cid}")
                elif action:
                    raise ValueError("labels cannot send actions")
                minimum, maximum = 0, 100
                initial = control.get("default")
                options: tuple[tuple[str, str], ...] = ()
                if ctype == "slider":
                    minimum, maximum = control.get("min", 0), control.get("max", 100)
                    if (type(minimum) is not int or type(maximum) is not int
                            or minimum < -100000 or maximum > 100000 or minimum >= maximum):
                        raise ValueError("invalid slider bounds")
                    if initial is None:
                        initial = minimum
                    if type(initial) is not int or not minimum <= initial <= maximum:
                        raise ValueError("invalid slider default")
                elif ctype == "toggle":
                    if initial is None:
                        initial = False
                    if type(initial) is not bool:
                        raise ValueError("invalid toggle default")
                elif ctype == "select":
                    raw_options = control.get("options", [])
                    if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= 100:
                        raise ValueError("invalid select options")
                    pairs: list[tuple[str, str]] = []
                    values: set[str] = set()
                    for option in raw_options:
                        if not isinstance(option, dict):
                            raise ValueError("invalid select option")
                        value = _id(option.get("value"), "select value")
                        if value in values:
                            raise ValueError("duplicate select value")
                        values.add(value)
                        pairs.append((_text(option.get("label"), "select label"), value))
                    options = tuple(pairs)
                    if initial is None:
                        initial = pairs[0][1]
                    if initial not in values:
                        raise ValueError("invalid select default")
                elif initial is not None:
                    raise ValueError("unexpected control default")
                validated.append(ExtensionControl(
                    cid, ctype, clabel, action, minimum, maximum, initial, options
                ))
            result.append(ExtensionSurface(module_id, sid, kind, label, icon, tuple(validated)))
    return tuple(result)


class ExtensionRegistry:
    """Only exposes validated surfaces for installed, enabled, live workers."""

    def __init__(self, registry, host):
        self.registry = registry
        self.host = host

    def active_surfaces(self, kind: str) -> tuple[ExtensionSurface, ...]:
        if kind not in _KINDS:
            raise ValueError(f"unknown extension kind: {kind}")
        result: list[ExtensionSurface] = []
        if kind == "remote" and not self.registry.is_enabled("remote"):
            return ()
        for manifest in self.registry.manifests():
            mid = manifest.module_id
            if (manifest.builtin or not self.registry.is_enabled(mid)
                    or not self.host.running(mid)):
                continue
            try:
                root = Path(self.registry.external_root) / mid
                raw = json.loads((root / "module.json").read_text(encoding="utf-8"))
                surfaces = validate_extensions(raw)
            except (OSError, ValueError, TypeError):
                continue  # fail closed for manually corrupted installs
            result.extend(surface for surface in surfaces if surface.kind == kind)
        return tuple(result)

    def action_allowed(self, module_id: str, surface_id: str, control_id: str, action: str) -> bool:
        return any(
            surface.module_id == module_id and surface.surface_id == surface_id
            and control.control_id == control_id and control.action == action
            and control.kind != "label"
            for kind in _KINDS for surface in self.active_surfaces(kind)
            for control in surface.controls
        )
