from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .shortcuts import GlobalShortcutCapability, ShortcutBinding


PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
HOST_REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"


@dataclass(slots=True, frozen=True)
class PortalShortcutSpec:
    shortcut_id: str
    description: str
    preferred_trigger: str = ""


@dataclass(slots=True, frozen=True)
class PortalProbeReport:
    capability: GlobalShortcutCapability
    session_bus_connected: bool = False
    host_registry_available: bool = False


_MODIFIERS = {
    "CTRL": "CTRL",
    "CONTROL": "CTRL",
    "ALT": "ALT",
    "SHIFT": "SHIFT",
    "META": "META",
    "SUPER": "META",
    "WIN": "META",
}

_KEY_ALIASES = {
    "ESC": "ESCAPE",
    "RETURN": "ENTER",
    "BACKSPACE": "BACKSPACE",
    "SPACE": "SPACE",
    "TAB": "TAB",
    "DELETE": "DELETE",
    "DEL": "DELETE",
    "INSERT": "INSERT",
    "INS": "INSERT",
    "HOME": "HOME",
    "END": "END",
    "PAGEUP": "PAGEUP",
    "PGUP": "PAGEUP",
    "PAGEDOWN": "PAGEDOWN",
    "PGDOWN": "PAGEDOWN",
    "UP": "UP",
    "DOWN": "DOWN",
    "LEFT": "LEFT",
    "RIGHT": "RIGHT",
}


def qt_sequence_to_xdg_trigger(sequence: str) -> str:
    """Convert the Qt PortableText form used by K-Sounds to an XDG trigger.

    K-Sounds stores shortcuts as strings such as ``Ctrl+B``.  The XDG
    shortcuts specification uses uppercase modifier names joined with ``+``.
    Keeping this conversion at the backend boundary lets the rest of K-Sounds
    remain desktop/backend neutral.
    """

    value = str(sequence or "").strip()
    if not value:
        return ""

    # Only the first chord is suitable for a GlobalShortcuts trigger. Qt can
    # represent multi-chord sequences separated by a comma; reject them rather
    # than silently registering a different shortcut.
    if "," in value:
        raise ValueError("XDG global shortcuts do not use multi-chord Qt sequences")

    parts = [part.strip() for part in value.split("+") if part.strip()]
    if not parts:
        return ""

    result: list[str] = []
    key: str | None = None

    for raw in parts:
        upper = raw.upper()
        modifier = _MODIFIERS.get(upper)
        if modifier is not None:
            if modifier not in result:
                result.append(modifier)
            continue

        if key is not None:
            raise ValueError(f"shortcut contains multiple non-modifier keys: {value}")

        if upper in _KEY_ALIASES:
            key = _KEY_ALIASES[upper]
        elif len(raw) == 1:
            key = raw.upper()
        elif upper.startswith("F") and upper[1:].isdigit():
            number = int(upper[1:])
            if 1 <= number <= 35:
                key = f"F{number}"
            else:
                raise ValueError(f"unsupported function key: {raw}")
        else:
            # Preserve established symbolic key names. The portal/compositor
            # remains the authority on whether a particular key is accepted.
            key = upper.replace(" ", "")

    if key is None:
        raise ValueError(f"shortcut has modifiers but no key: {value}")

    return "+".join((*result, key))


def portal_specs(bindings: Iterable[ShortcutBinding]) -> tuple[PortalShortcutSpec, ...]:
    result: list[PortalShortcutSpec] = []
    for binding in bindings:
        if not binding.enabled or not binding.trigger.strip():
            continue
        result.append(
            PortalShortcutSpec(
                shortcut_id=binding.shortcut_id,
                description=binding.description,
                preferred_trigger=qt_sequence_to_xdg_trigger(binding.trigger),
            )
        )
    return tuple(result)


def probe_xdg_global_shortcuts_portal() -> PortalProbeReport:
    """Live, side-effect-free probe of the user's desktop portal.

    This intentionally does *not* create a GlobalShortcuts session and cannot
    display a permission/configuration dialog. It only verifies QtDBus,
    the session bus and the portal interfaces needed by the later binding step.
    """

    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except Exception as exc:  # pragma: no cover - host dependent
        return PortalProbeReport(
            capability=GlobalShortcutCapability(
                available=False,
                backend="xdg-portal",
                detail=f"QtDBus unavailable: {exc}",
            )
        )

    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return PortalProbeReport(
                capability=GlobalShortcutCapability(
                    available=False,
                    backend="xdg-portal",
                    detail="D-Bus session bus unavailable",
                ),
                session_bus_connected=False,
            )

        portal = QDBusInterface(
            PORTAL_SERVICE,
            PORTAL_PATH,
            PORTAL_INTERFACE,
            bus,
        )
        registry = QDBusInterface(
            PORTAL_SERVICE,
            PORTAL_PATH,
            HOST_REGISTRY_INTERFACE,
            bus,
        )

        if not portal.isValid():
            error = portal.lastError().message() if portal.lastError().isValid() else ""
            return PortalProbeReport(
                capability=GlobalShortcutCapability(
                    available=False,
                    backend="xdg-portal",
                    detail=error or "GlobalShortcuts portal interface unavailable",
                ),
                session_bus_connected=True,
                host_registry_available=bool(registry.isValid()),
            )

        raw_version = portal.property("version")
        try:
            version = int(raw_version)
        except Exception:
            version = 0

        return PortalProbeReport(
            capability=GlobalShortcutCapability(
                available=True,
                version=version,
                backend="xdg-portal",
                detail=PORTAL_INTERFACE,
            ),
            session_bus_connected=True,
            host_registry_available=bool(registry.isValid()),
        )
    except Exception as exc:  # pragma: no cover - host dependent
        return PortalProbeReport(
            capability=GlobalShortcutCapability(
                available=False,
                backend="xdg-portal",
                detail=str(exc),
            )
        )
