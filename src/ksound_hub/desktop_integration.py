from __future__ import annotations

import os
from pathlib import Path


CANONICAL_DESKTOP_ID = "k-sounds-hub.desktop"
AUTOSTART_DESKTOP_IDS = (
    CANONICAL_DESKTOP_ID,
    "ksounds-hub.desktop",
    "ksounds-hub-glass.desktop",
    "k-sounds-hub-glass.desktop",
    "ksound-hub.desktop",
    "ksound-hub-glass.desktop",
    "ksound-hub-v2.desktop",
)


def _xdg_home(environment_name: str, fallback: Path) -> Path:
    configured = os.environ.get(environment_name, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
    return fallback


def autostart_directory() -> Path:
    return _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / "autostart"


def canonical_wrapper_path() -> Path:
    return Path.home() / ".local" / "bin" / "k-sounds-hub"


def _desktop_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    values: dict[str, str] = {}
    in_desktop_entry = False
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_desktop_entry = line.casefold() == "[desktop entry]"
            continue
        if not in_desktop_entry or not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().casefold()] = value.strip()
    return values


def desktop_entry_is_active(path: Path) -> bool:
    if not path.is_file():
        return False
    values = _desktop_values(path)
    if values.get("type", "").casefold() != "application":
        return False
    if not values.get("exec", "").strip():
        return False
    if values.get("hidden", "false").casefold() == "true":
        return False
    if values.get("x-gnome-autostart-enabled", "true").casefold() == "false":
        return False
    return True


def autostart_enabled(directory: Path | None = None) -> bool:
    target = directory or autostart_directory()
    return any(desktop_entry_is_active(target / name) for name in AUTOSTART_DESKTOP_IDS)


def _autostart_document(wrapper: Path, icon: Path | None = None) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=K-Sounds Hub",
        "Comment=Start the K-Sounds Hub Glass frontend",
        f"Exec={wrapper}",
        f"TryExec={wrapper}",
    ]
    if icon is not None and icon.is_file():
        lines.append(f"Icon={icon}")
    lines.extend(
        (
            "Terminal=false",
            "StartupNotify=false",
            "X-GNOME-Autostart-enabled=true",
            "X-KDE-autostart-after=panel",
            "",
        )
    )
    return "\n".join(lines)


def set_autostart_enabled(
    enabled: bool,
    *,
    directory: Path | None = None,
    wrapper: Path | None = None,
    icon: Path | None = None,
) -> bool:
    target = directory or autostart_directory()
    canonical = target / CANONICAL_DESKTOP_ID

    if not enabled:
        for name in AUTOSTART_DESKTOP_IDS:
            try:
                (target / name).unlink()
            except FileNotFoundError:
                pass
        return autostart_enabled(target)

    wrapper_path = (wrapper or canonical_wrapper_path()).expanduser()
    if not wrapper_path.is_file():
        raise FileNotFoundError(f"K-Sounds launcher not found: {wrapper_path}")
    if not os.access(wrapper_path, os.X_OK):
        raise PermissionError(f"K-Sounds launcher is not executable: {wrapper_path}")

    if icon is None:
        data_home = _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share")
        icon = data_home / "k-sounds-hub" / "app" / "src" / "ksound_hub" / "assets" / "app_icon.png"

    target.mkdir(parents=True, exist_ok=True)
    temporary = target / f".{CANONICAL_DESKTOP_ID}.{os.getpid()}.tmp"
    try:
        temporary.write_text(_autostart_document(wrapper_path, icon), encoding="utf-8")
        temporary.chmod(0o755)
        for name in AUTOSTART_DESKTOP_IDS:
            if name == CANONICAL_DESKTOP_ID:
                continue
            try:
                (target / name).unlink()
            except FileNotFoundError:
                pass
        temporary.replace(canonical)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    return autostart_enabled(target)
