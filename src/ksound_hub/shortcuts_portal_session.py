from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import secrets


PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"

CANONICAL_DESKTOP_ID = "k-sounds-hub.desktop"
CANONICAL_APP_ID = "k-sounds-hub"


@dataclass(slots=True, frozen=True)
class PortalSessionIdentity:
    app_id: str
    desktop_id: str
    desktop_file: Path | None


@dataclass(slots=True, frozen=True)
class PortalSessionProbeResult:
    ok: bool
    app_id: str = ""
    session_handle: str = ""
    registry_registered: bool = False
    create_response: int | None = None
    detail: str = ""


def app_id_from_desktop_id(desktop_id: str) -> str:
    value = str(desktop_id or "").strip()
    if value.endswith(".desktop"):
        value = value[:-8]
    if not value:
        raise ValueError("desktop id must not be empty")
    return value


def _application_search_roots() -> tuple[Path, ...]:
    roots: list[Path] = []

    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        roots.append(Path(xdg_data_home).expanduser() / "applications")
    else:
        roots.append(Path.home() / ".local/share/applications")

    for raw in os.environ.get(
        "XDG_DATA_DIRS",
        "/usr/local/share:/usr/share",
    ).split(":"):
        raw = raw.strip()
        if raw:
            roots.append(Path(raw).expanduser() / "applications")

    # Preserve order while deduplicating.
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return tuple(unique)


def resolve_portal_identity(
    desktop_id: str = CANONICAL_DESKTOP_ID,
) -> PortalSessionIdentity:
    app_id = app_id_from_desktop_id(desktop_id)
    for root in _application_search_roots():
        candidate = root / desktop_id
        if candidate.is_file():
            return PortalSessionIdentity(
                app_id=app_id,
                desktop_id=desktop_id,
                desktop_file=candidate,
            )

    return PortalSessionIdentity(
        app_id=app_id,
        desktop_id=desktop_id,
        desktop_file=None,
    )


def portal_token(prefix: str) -> str:
    """Return a valid, collision-resistant D-Bus object-path element."""

    clean = re.sub(r"[^A-Za-z0-9_]", "_", str(prefix or "ksounds"))
    if not clean or clean[0].isdigit():
        clean = f"k_{clean}"
    return f"{clean}_{secrets.token_hex(6)}"


def sender_path_component(base_service: str) -> str:
    """Convert ':1.234' to the XDG request/session path sender component."""

    value = str(base_service or "").strip()
    if value.startswith(":"):
        value = value[1:]
    value = value.replace(".", "_")
    value = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not value:
        raise ValueError("D-Bus base service is empty")
    return value


def expected_request_path(base_service: str, handle_token: str) -> str:
    return (
        f"{PORTAL_PATH}/request/"
        f"{sender_path_component(base_service)}/"
        f"{handle_token}"
    )


def create_session_options(handle_token: str, session_handle_token: str) -> dict[str, str]:
    # PySide6/QtDBus already wraps Python values as the variants required by
    # a{sv}. The inner values therefore must stay plain strings. Manually
    # wrapping them in QDBusVariant produces variant-of-variant (v) where the
    # portal contract requires the inner type to be string (s).
    handle = str(handle_token or "").strip()
    session = str(session_handle_token or "").strip()
    if not handle or not session:
        raise ValueError("portal session tokens must not be empty")
    return {
        "handle_token": handle,
        "session_handle_token": session,
    }


def unwrap_dbus_value(value):
    """Best-effort unwrapping for QDBusVariant/QDBusObjectPath-like values."""

    variant = getattr(value, "variant", None)
    if callable(variant):
        try:
            return unwrap_dbus_value(variant())
        except Exception:
            pass

    path = getattr(value, "path", None)
    if callable(path):
        try:
            return str(path())
        except Exception:
            pass

    if isinstance(value, dict):
        return {str(k): unwrap_dbus_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(unwrap_dbus_value(v) for v in value)
    return value
