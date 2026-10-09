#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import mimetypes
import hashlib
import os
import tempfile
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

_SRC_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
)

if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from ksound_hub.module_remote_bridge import (
    read as read_remote_modules,
    control_for,
    valid_value,
    FILE_NAME as REMOTE_SURFACES_FILE,
)
from ksound_hub.module_remote_bridge import RECEIPT_DIR, receipt_begin, receipt_read, receipt_finish

CONFIG_DIR = Path(os.environ.get("KSH_CONFIG_DIR", str(Path.home() / ".config" / "k-sounds-hub"))).expanduser()
CONFIG_PATH = CONFIG_DIR / "soundboard.json"
TOKEN_PATH = CONFIG_DIR / "soundboard_web_token"
PAIRING_PATH = CONFIG_DIR / "soundboard_pairing.json"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
FAVORITES_PATH = CONFIG_DIR / "remote_favorites.json"

MODULE_REMOTE_PATH = CONFIG_DIR / REMOTE_SURFACES_FILE
MODULE_RECEIPTS_PATH = CONFIG_DIR / RECEIPT_DIR
MODULE_JS = Path(__file__).with_name("module_grafts.js")

MODULE_CSS = """
.module-switch button[hidden] {
    display:none!important;
}

.external-module-panel[hidden] {
    display:none!important;
}

.external-module-panel {
    padding:16px;
    display:grid;
    gap:12px;
}

.external-module-card {
    padding:14px;
    border:1px solid rgba(62,216,255,.25);
    border-radius:16px;
    background:rgba(10,20,32,.85);
    display:grid;
    gap:10px;
}

.external-module-card label {
    display:grid;
    gap:6px;
}

.external-module-card button {
    min-height:43px;
    border-radius:12px;
}

.external-module-card button:active {
    transform:translateY(2px);
}
"""

HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("KSOUND_SOUNDBOARD_WEB_PORT", "8765"))
DISCOVERY_PORT = int(os.environ.get("KSOUND_SOUNDBOARD_DISCOVERY_PORT", "8766"))
REMOTE_IDLE_TIMEOUT_SECONDS = max(0, int(os.environ.get("KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS", "0")))
REMOTE_IDLE_CHECK_SECONDS = max(5, int(os.environ.get("KSOUND_SOUNDBOARD_IDLE_CHECK_SECONDS", "30")))


def _remote_surfaces_from_environment() -> tuple[str, ...]:
    allowed = ("pads", "voice_fx")
    raw = os.environ.get("KSH_REMOTE_SURFACES", "pads,voice_fx")
    requested = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return tuple(item for item in allowed if item in requested)


REMOTE_SURFACES = _remote_surfaces_from_environment()

def remote_surface_enabled(surface: str) -> bool:
    return str(surface or "").strip().lower() in REMOTE_SURFACES


_REMOTE_LAST_ACTIVITY = time.monotonic()
_REMOTE_ACTIVITY_LOCK = threading.Lock()
_FAVORITES_LOCK = threading.RLock()
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE_SIGNATURE: tuple[int, int] | None = None
_CONFIG_CACHE_DOCUMENT: dict = {}
_STATE_CACHE_LOCK = threading.Lock()
_STATE_CACHE_SIGNATURE: tuple[int, int] | None = None
_STATE_CACHE_DOCUMENT: dict | None = None


def mark_remote_activity(reason: str = "") -> None:
    global _REMOTE_LAST_ACTIVITY
    with _REMOTE_ACTIVITY_LOCK:
        _REMOTE_LAST_ACTIVITY = time.monotonic()



SERVICE_NAME = "K-Sounds Soundboard Remote"
DISCOVERY_REQUEST = b"KSH_DISCOVER_V2"
DISCOVERY_REQUESTS = {b"KSH_DISCOVER_V1", b"KSH_DISCOVER_V2"}
DISCOVERY_SERVICE = "KSH_SOUNDBOARD"

VOICE_FX_DEFINITIONS = (
    ("distortion", "Distortion", "⚡"),
    ("pitch_down", "Pitch ↓", "▼"),
    ("pitch_up", "Pitch ↑", "▲"),
    ("radio", "Radio Pilot", "◉"),
    ("telephone", "Telephone", "☎"),
    ("robot", "Robot 1", "◇"),
    ("reverb", "Reverb", "≈"),
    ("church", "Church", "✦"),
    ("church_pa", "Church PA", "▣"),
)
VOICE_FX_ORDER = tuple(item[0] for item in VOICE_FX_DEFINITIONS)


PAD_AUTO_PALETTES = (
    ("#18487e", "#5a2d8e"),
    ("#126069", "#1e448e"),
    ("#732f56", "#304191"),
    ("#7a421c", "#7d234f"),
    ("#256546", "#214182"),
    ("#493784", "#166374"),
    ("#7e2b31", "#463089"),
    ("#25498f", "#702973"),
)


def automatic_pad_colors(seed: str) -> tuple[str, str]:
    digest = hashlib.sha256(str(seed or "sound").encode("utf-8", "ignore")).digest()
    return PAD_AUTO_PALETTES[int.from_bytes(digest[:2], "big") % len(PAD_AUTO_PALETTES)]


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except Exception:
        pass


def ensure_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if TOKEN_PATH.is_file():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            chmod_private(TOKEN_PATH)
            return token

    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    chmod_private(TOKEN_PATH)
    return token


TOKEN = ensure_token()


# Public-release security: limit PIN guessing to five trials in 300 s per
# TCP peer. The legacy LAN pairing API stays compatible with existing Android
# clients; future releases can migrate to authenticated HTTPS requests.
from ipaddress import ip_address, ip_network

_PAIR_RATE_LOCK = threading.Lock()
_PAIR_ATTEMPTS: dict[str, tuple[float, int]] = {}
_PAIR_RATE_LIMIT = 5
_PAIR_RATE_WINDOW_SECONDS = 300
_ALLOWED_LAN = tuple(ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _remote_client_allowed(client_ip: str) -> bool:
    """Reject direct public-Internet peers; Remote is LAN-only by default."""
    if os.environ.get("KSH_REMOTE_ALLOW_PUBLIC_CLIENTS", "").lower() in ("1", "yes", "true"):
        return True
    try:
        addr = ip_address(client_ip)
        return any(addr in network for network in _ALLOWED_LAN)
    except ValueError:
        return False


def _pairing_rate_limit_allows(client_ip: str, now: float | None = None) -> bool:
    if now is None:
        now = time.monotonic()
    with _PAIR_RATE_LOCK:
        # Bounded state: stale buckets vanish and a large number of distinct
        # incoming peers cannot grow this dictionary indefinitely.
        if len(_PAIR_ATTEMPTS) > 256:
            for peer, (deadline, _) in list(_PAIR_ATTEMPTS.items()):
                if deadline <= now:
                    _PAIR_ATTEMPTS.pop(peer, None)
            if len(_PAIR_ATTEMPTS) > 256:
                return False
        until, attempts = _PAIR_ATTEMPTS.get(client_ip, (0.0, 0))
        if now >= until:
            until, attempts = now + _PAIR_RATE_WINDOW_SECONDS, 0
        if attempts >= _PAIR_RATE_LIMIT:
            return False
        _PAIR_ATTEMPTS[client_ip] = (until, attempts + 1)
        return True


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def read_slots() -> list[dict]:
    data = read_soundboard_document()
    slots = data.get("slots", [])
    return [slot for slot in slots if isinstance(slot, dict)]


def read_soundboard_document() -> dict:
    """Read soundboard.json once per file revision for all HTTP threads."""
    global _CONFIG_CACHE_DOCUMENT, _CONFIG_CACHE_SIGNATURE
    try:
        stat = CONFIG_PATH.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        signature = None

    with _CONFIG_CACHE_LOCK:
        if signature is not None and signature == _CONFIG_CACHE_SIGNATURE:
            return _CONFIG_CACHE_DOCUMENT

        data = {}
        if signature is not None:
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                elif isinstance(loaded, list):
                    data = {"slots": loaded}
            except Exception:
                data = {}

        _CONFIG_CACHE_SIGNATURE = signature
        _CONFIG_CACHE_DOCUMENT = data
        return _CONFIG_CACHE_DOCUMENT


def invalidate_soundboard_document_cache() -> None:
    global _CONFIG_CACHE_DOCUMENT, _CONFIG_CACHE_SIGNATURE
    global _STATE_CACHE_DOCUMENT, _STATE_CACHE_SIGNATURE
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE_SIGNATURE = None
        _CONFIG_CACHE_DOCUMENT = {}
    with _STATE_CACHE_LOCK:
        _STATE_CACHE_SIGNATURE = None
        _STATE_CACHE_DOCUMENT = None


def clean_folder_label(value) -> str:
    label = " ".join(str(value or "").strip().split())
    if not label:
        return ""
    if label.casefold() == "main":
        return "Main"
    return label[:48]


def slot_folder_label(slot: dict) -> str:
    if not isinstance(slot, dict):
        return ""
    return clean_folder_label(slot.get("folder"))


def available_soundboard_folders(data: dict) -> list[str]:
    folders: list[str] = []

    def add_folder(value) -> None:
        label = clean_folder_label(value)
        if not label:
            return
        if all(existing.casefold() != label.casefold() for existing in folders):
            folders.append(label)

    add_folder("Main")

    raw_folders = data.get("folders", []) if isinstance(data, dict) else []
    if isinstance(raw_folders, list):
        for folder in raw_folders:
            add_folder(folder)

    raw_slots = data.get("slots", []) if isinstance(data, dict) else []
    if isinstance(raw_slots, list):
        for slot in raw_slots:
            if isinstance(slot, dict):
                add_folder(slot.get("folder"))

    return folders


def background_file_version(path_text: str) -> str:
    path_text = str(path_text or "").strip()
    if not path_text:
        return ""

    try:
        image_path = Path(path_text).expanduser()
        stat = image_path.stat()
        if not image_path.is_file():
            return ""
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except Exception:
        return ""


def read_soundboard_settings() -> dict:
    defaults = {
        "global_volume": 100,
        "auto_level_enabled": False,
        "pad_scale": 100,
        "remote_columns": 0,
        "remote_contrast": "normal",
        "remote_background": "glass",
        "remote_folder_size": "large",
        "remote_folder_style": "pills",
        "remote_pinch_outside_edit": False,
    }

    data = read_soundboard_document()
    if not data:
        return defaults

    try:
        defaults["global_volume"] = max(0, min(100, int(data.get("global_volume", 100))))
    except Exception:
        defaults["global_volume"] = 100

    defaults["auto_level_enabled"] = bool(data.get("auto_level_enabled", False))

    try:
        defaults["pad_scale"] = max(35, min(130, int(data.get("pad_scale", 100))))
    except Exception:
        defaults["pad_scale"] = 100

    try:
        raw_columns = int(data.get("remote_columns", 0))
        defaults["remote_columns"] = max(1, min(13, raw_columns)) if raw_columns else 0
    except Exception:
        defaults["remote_columns"] = 0

    contrast = str(data.get("remote_contrast") or defaults["remote_contrast"]).strip().lower()
    defaults["remote_contrast"] = contrast if contrast in {"normal", "high", "soft"} else "normal"

    background = str(data.get("remote_background") or defaults["remote_background"]).strip().lower()
    defaults["remote_background"] = background if background in {"glass", "clean", "grid", "dark"} else "glass"

    folder_size = str(data.get("remote_folder_size") or defaults["remote_folder_size"]).strip().lower()
    defaults["remote_folder_size"] = folder_size if folder_size in {"normal", "large", "xl"} else "large"

    folder_style = str(data.get("remote_folder_style") or defaults["remote_folder_style"]).strip().lower()
    defaults["remote_folder_style"] = folder_style if folder_style in {"pills", "tabs", "drawer"} else "pills"
    raw_pinch = data.get("remote_pinch_outside_edit", False)
    if isinstance(raw_pinch, bool):
        defaults["remote_pinch_outside_edit"] = raw_pinch
    else:
        defaults["remote_pinch_outside_edit"] = str(raw_pinch or "").strip().lower() in {"1", "true", "yes", "on"}

    return defaults


def read_voice_fx_state() -> dict:
    """Read persisted MICRO Voice FX state while Glass remains the only writer."""
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {}
    except Exception:
        data = {}
    channels = data.get("channels", []) if isinstance(data, dict) else []
    micro = next((item for item in channels if isinstance(item, dict) and str(item.get("key") or "").strip().lower() == "micro"), {})
    raw_active = micro.get("micro_voice_fx_active", []) if isinstance(micro, dict) else []
    requested = {str(item or "").strip().lower() for item in raw_active} if isinstance(raw_active, list) else set()
    active = [key for key in VOICE_FX_ORDER if key in requested]
    if not active and isinstance(micro, dict) and bool(micro.get("micro_voice_fx_enabled", False)):
        legacy = str(micro.get("micro_voice_fx_preset") or "radio").strip().lower()
        active = [legacy if legacy in VOICE_FX_ORDER else "radio"]
    try:
        amount = max(0, min(100, int(micro.get("micro_voice_fx_amount", 72))))
    except Exception:
        amount = 72
    active_set = set(active)
    return {
        "active": active,
        "enabled": bool(active),
        "amount": amount,
        "effects": [
            {"id": key, "label": label, "symbol": symbol, "active": key in active_set}
            for key, label, symbol in VOICE_FX_DEFINITIONS
        ],
    }


# Favorites belong to the PC-side Remote, not Android localStorage or MICRO settings.
# Keep one ordered list so Pads and Voice FX can be mixed in the quick-access rail.
MAX_REMOTE_FAVORITES = 4096  # defensive JSON bound, not a user-facing favorites limit


def normalize_remote_favorites(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        identifier = str(item.get("id") or "").strip()
        if kind not in {"pad", "voice_fx"} or not identifier or len(identifier) > 128:
            continue
        if kind == "voice_fx":
            identifier = identifier.lower()
            if identifier not in VOICE_FX_ORDER:
                continue
        pair = (kind, identifier)
        if pair in seen:
            continue
        seen.add(pair)
        result.append({"type": kind, "id": identifier})
        if len(result) >= MAX_REMOTE_FAVORITES:
            break
    return result


def read_remote_favorites() -> list[dict]:
    with _FAVORITES_LOCK:
        try:
            data = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return normalize_remote_favorites(data.get("items") if isinstance(data, dict) else None)


def save_remote_favorites(items: list[dict]) -> list[dict]:
    normalized = normalize_remote_favorites(items)
    # Validate PAD identifiers against current soundboard state. A deleted pad
    # can remain in an existing saved list, but it cannot be newly pinned.
    existing = {(item["type"], item["id"]) for item in read_remote_favorites()}
    valid_pad_ids = {str(item.get("id") or "") for item in read_soundboard_state().get("slots", [])}
    for item in normalized:
        if item["type"] == "pad" and item["id"] not in valid_pad_ids and ("pad", item["id"]) not in existing:
            raise ValueError("Unknown pad")
    with _FAVORITES_LOCK:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic replace; user settings and soundboard.json stay untouched.
        fd, name = tempfile.mkstemp(prefix=".remote-favorites-", suffix=".json", dir=CONFIG_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": normalized}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, FAVORITES_PATH)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return normalized


def _state_with_live_voice_fx(base: dict) -> dict:
    state = dict(base) if isinstance(base, dict) else {}
    state["voice_fx"] = read_voice_fx_state()
    state["favorites"] = read_remote_favorites()
    return state


def soundboard_revision() -> str:
    data = read_soundboard_document()
    if not data:
        return "missing"

    slots = data.get("slots", []) if isinstance(data, dict) else []
    folders = data.get("folders", []) if isinstance(data, dict) else []
    payload = json.dumps(
        {
            "slots": slots,
            "folders": folders,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def read_soundboard_state() -> dict:
    global _STATE_CACHE_DOCUMENT, _STATE_CACHE_SIGNATURE
    data = read_soundboard_document()
    config_signature = _CONFIG_CACHE_SIGNATURE
    with _STATE_CACHE_LOCK:
        if (
            config_signature is not None
            and config_signature == _STATE_CACHE_SIGNATURE
            and _STATE_CACHE_DOCUMENT is not None
        ):
            return _state_with_live_voice_fx(_STATE_CACHE_DOCUMENT)

    raw_slots = data.get("slots", [])
    if not isinstance(raw_slots, list):
        raw_slots = []

    slots = []
    for index, raw_slot in enumerate(raw_slots):
        if not isinstance(raw_slot, dict):
            continue

        slot_id = str(raw_slot.get("id") or f"sb{index + 1}").strip() or f"sb{index + 1}"
        label = str(raw_slot.get("label") or f"SOUND {index + 1:02d}").strip() or f"SOUND {index + 1:02d}"
        path = str(raw_slot.get("path") or "").strip()
        background_path = str(raw_slot.get("background_path") or "").strip()
        background_version = background_file_version(background_path)
        folder_label = slot_folder_label(raw_slot)
        auto_colors = automatic_pad_colors(slot_id or label)

        try:
            volume = max(0, min(100, int(raw_slot.get("volume", 80))))
        except Exception:
            volume = 80

        output_channel = str(raw_slot.get("output_channel") or "media").strip().lower() or "media"

        slots.append(
            {
                "id": slot_id,
                "index": index,
                "number": index + 1,
                "label": label,
                "path": path,
                "background_path": background_path,
                "background_version": background_version,
                "has_sound": bool(path),
                "has_background": bool(background_version),
                "auto_colors": list(auto_colors),
                "volume": volume,
                "shortcut": str(raw_slot.get("shortcut") or "").strip(),
                "output_channel": output_channel,
                "folder": folder_label,
            }
        )

    folders = available_soundboard_folders(data)
    revision_payload = {
        "slots": slots,
        "folders": folders,
        "slot_count": len(slots),
    }
    revision = hashlib.sha1(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    settings = read_soundboard_settings()
    state = {
        "revision": revision,
        "global_volume": int(data.get("global_volume", 100)),
        "auto_level_enabled": bool(data.get("auto_level_enabled", False)),
        "pad_scale": int(data.get("pad_scale", 100)),
        "remote_columns": settings.get("remote_columns", 0),
        "folders": folders,
        "slot_count": len(slots),
        "slots": slots,
    }
    with _STATE_CACHE_LOCK:
        _STATE_CACHE_SIGNATURE = config_signature
        _STATE_CACHE_DOCUMENT = state
    return _state_with_live_voice_fx(state)


def update_soundboard_setting(key: str, value) -> None:
    # The cached document is shared by HTTP worker threads; detach before any
    # mutation and atomically replace the file only after serialization.
    loaded = json.loads(json.dumps(read_soundboard_document(), ensure_ascii=False))

    if isinstance(loaded, list):
        data = {"slots": loaded}
    elif isinstance(loaded, dict):
        data = loaded
    else:
        data = {"slots": []}

    if not isinstance(data.get("slots"), list):
        data["slots"] = []

    if key == "global_volume":
        try:
            data[key] = max(0, min(100, int(value)))
        except Exception:
            data[key] = 100
    elif key == "auto_level_enabled":
        data[key] = bool(value)
    elif key == "pad_scale":
        try:
            data[key] = max(35, min(130, int(value)))
        except Exception:
            data[key] = 100
    elif key == "remote_columns":
        try:
            raw_columns = int(value)
            data[key] = max(1, min(13, raw_columns)) if raw_columns else 0
        except Exception:
            data[key] = 0
    elif key == "remote_contrast":
        cleaned = str(value or "").strip().lower()
        data[key] = cleaned if cleaned in {"normal", "high", "soft"} else "normal"
    elif key == "remote_background":
        cleaned = str(value or "").strip().lower()
        data[key] = cleaned if cleaned in {"glass", "clean", "grid", "dark"} else "glass"
    elif key == "remote_folder_size":
        cleaned = str(value or "").strip().lower()
        data[key] = cleaned if cleaned in {"normal", "large", "xl"} else "large"
    elif key == "remote_folder_style":
        cleaned = str(value or "").strip().lower()
        data[key] = cleaned if cleaned in {"pills", "tabs", "drawer"} else "pills"
    elif key == "remote_pinch_outside_edit":
        if isinstance(value, bool):
            data[key] = value
        else:
            data[key] = str(value or "").strip().lower() in {"1", "true", "yes", "on"}
    else:
        return

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_name(
        f".{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    invalidate_soundboard_document_cache()


def send_ipc(payload: dict) -> tuple[bool, str]:
    # Bridge remote web actions to the running Glass IPC server.
    # Keep compatibility with current and historical socket names.
    candidates = [
        os.environ.get("KSH_IPC_SOCKET_PATH", ""),
        os.environ.get("KSOUND_HUB_IPC_SOCKET", ""),
        f"/tmp/ksounds_hub_audio_{os.getuid()}.sock",
        f"/tmp/ksound_hub_audio_v2_{os.getuid()}.sock",
        f"/tmp/ksound_hub_audio_{os.getuid()}.sock",
    ]

    seen: set[str] = set()
    last_error = ""
    for socket_path in [candidate for candidate in candidates if candidate]:
        if socket_path in seen:
            continue
        seen.add(socket_path)

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.45)
                sock.connect(socket_path)
                sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            return True, "OK"
        except Exception as exc:
            last_error = f"{socket_path}: {exc}"

    return False, last_error or "IPC unavailable"


def read_valid_pairing() -> dict | None:
    if not PAIRING_PATH.is_file():
        return None
    try:
        data = json.loads(PAIRING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    try:
        expires_at = float(data.get("expires_at", 0))
    except Exception:
        return None

    if time.time() > expires_at:
        try:
            PAIRING_PATH.unlink()
        except Exception:
            pass
        return None

    pin = str(data.get("pin", "")).strip()
    if not pin:
        return None

    return data


def consume_pairing_if_pin_matches(pin: str) -> bool:
    data = read_valid_pairing()
    if not data:
        return False

    expected = str(data.get("pin", "")).strip()
    provided = str(pin or "").strip()

    if not expected or not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return False

    try:
        PAIRING_PATH.unlink()
    except Exception:
        pass

    return True


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def conditional_json_response(handler: BaseHTTPRequestHandler, payload: dict) -> None:
    """Allow the Remote device to revalidate state without retransferring it."""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = '"state-' + hashlib.sha256(data).hexdigest() + '"'
    if str(handler.headers.get("If-None-Match") or "").strip() == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "private, no-cache, max-age=0")
        handler.end_headers()
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "private, no-cache, max-age=0")
    handler.send_header("ETag", etag)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def page(status: str = "") -> bytes:
    pads_available = remote_surface_enabled("pads")
    voice_fx_available = remote_surface_enabled("voice_fx")
    available_remote_modes = [
        mode for mode, available in (("pads", pads_available), ("voice", voice_fx_available)) if available
    ]
    default_remote_mode = available_remote_modes[0] if available_remote_modes else "pads"
    module_switch_buttons = []
    if pads_available:
        module_switch_buttons.append(
            '<button id="modulePadsButton" type="button" onclick="setRemoteMode(\'pads\')">▦ Pads</button>'
        )
    if voice_fx_available:
        module_switch_buttons.append(
            '<button id="moduleVoiceButton" type="button" onclick="setRemoteMode(\'voice\')">✦ Voice FX</button>'
        )
    module_switch_html = (
        '<nav class="module-switch" aria-label="Remote module">'
        + ''.join(module_switch_buttons)
        + '</nav>'
        if len(module_switch_buttons) > 1
        else ''
    )
    remote_subtitle = (
        "K-Sounds Remote"
        if len(available_remote_modes) > 1
        else ("Voice FX Remote" if default_remote_mode == "voice" else "Pads Remote")
    )
    initial_gesture_hint = (
        "Tap an effect to toggle it · Active effects stay synchronized with K-Sounds"
        if default_remote_mode == "voice"
        else "Tap to play. Hold and drag a pad onto another pad to swap positions."
    )
    pads_hidden_attr = "" if default_remote_mode == "pads" else " hidden"
    voice_hidden_attr = "" if default_remote_mode == "voice" else " hidden"

    state = read_soundboard_state()
    slots = state.get("slots", [])
    folders = state.get("folders", ["Main"])
    settings = read_soundboard_settings()
    config_revision = html.escape(str(state.get("revision", "")), quote=True)
    global_volume = int(settings.get("global_volume", 100))
    auto_level_text = "ON" if settings.get("auto_level_enabled") else "OFF"

    pad_scale = max(35, min(130, int(settings.get("pad_scale", 100))))
    remote_columns = max(0, min(13, int(settings.get("remote_columns", 0))))
    remote_contrast = str(settings.get("remote_contrast", "normal")).strip().lower()
    remote_background = str(settings.get("remote_background", "glass")).strip().lower()
    remote_folder_size = str(settings.get("remote_folder_size", "large")).strip().lower()
    remote_folder_style = str(settings.get("remote_folder_style", "pills")).strip().lower()
    remote_pinch_outside_edit = bool(settings.get("remote_pinch_outside_edit", False))

    if remote_contrast not in {"normal", "high", "soft"}:
        remote_contrast = "normal"
    if remote_background not in {"glass", "clean", "grid", "dark"}:
        remote_background = "glass"
    if remote_folder_size not in {"normal", "large", "xl"}:
        remote_folder_size = "large"
    if remote_folder_style not in {"pills", "tabs", "drawer"}:
        remote_folder_style = "pills"

    body_classes = " ".join(
        [
            f"contrast-{remote_contrast}",
            f"remote-bg-{remote_background}",
            f"folder-size-{remote_folder_size}",
            f"folder-style-{remote_folder_style}",
            *( ["remote-mode-voice"] if default_remote_mode == "voice" else [] ),
        ]
    )

    def option_selected(current: str, wanted: str) -> str:
        return " selected" if str(current).lower() == wanted else ""

    pad_factor = pad_scale / 100.0
    pad_min_width = max(92, int(126 * pad_factor))
    pad_min_height = max(86, int(108 * pad_factor))
    pad_padding = max(8, int(14 * pad_factor))
    pad_radius = max(14, int(20 * pad_factor))
    pad_wrap_radius = max(16, int(22 * pad_factor))
    pad_gap = max(5, int(9 * pad_factor))
    pad_title_font = max(13, int(17 * pad_factor))
    pad_subtitle_font = max(10, int(12 * pad_factor))
    pad_title_margin = max(4, int(8 * pad_factor))

    escaped_token = html.escape(TOKEN, quote=True)

    folder_buttons = [
        '<button type="button" class="folder-pill active" data-folder="__all__">All</button>'
    ]
    for folder in folders:
        folder_label = clean_folder_label(folder)
        if not folder_label:
            continue
        escaped_folder = html.escape(folder_label, quote=True)
        folder_filter = "" if folder_label.casefold() == "main" else folder_label
        escaped_filter = html.escape(folder_filter, quote=True)
        folder_buttons.append(
            f'<button type="button" class="folder-pill" data-folder="{escaped_filter}">{escaped_folder}</button>'
        )

    folder_bar_html = (
        '<section class="folder-bar" aria-label="Sound folders">'
        '<button class="folder-settings" type="button" onclick="openFolderOptions()" '
        'aria-label="Folder layout options">Folders ⚙</button>'
        + "".join(folder_buttons)
        + "</section>"
    )

    buttons = []

    for index, slot in enumerate(slots):
        slot_id = str(slot.get("id") or f"sb{index + 1}")
        label = str(slot.get("label") or slot_id)
        folder_label = clean_folder_label(slot.get("folder"))
        path_text = str(slot.get("path") or "")
        background_path = str(slot.get("background_path") or "").strip()
        background_version = background_file_version(background_path)
        background_exists = bool(background_version)
        colors = slot.get("auto_colors") if isinstance(slot.get("auto_colors"), list) else []
        if len(colors) != 2:
            colors = list(automatic_pad_colors(slot_id or label))
        disabled = "" if path_text else "disabled"
        subtitle = Path(path_text).name if path_text else "No sound"
        pad_class = "pad"
        style_attr = (
            f' style="--pad-auto-a: {html.escape(str(colors[0]), quote=True)}; '
            f'--pad-auto-b: {html.escape(str(colors[1]), quote=True)}"'
        )
        background_url = ""
        if background_exists:
            background_url = (
                "/background?token="
                + quote(TOKEN, safe="")
                + "&slot="
                + quote(slot_id, safe="")
                + "&rev="
                + quote(background_version, safe="")
            )

        buttons.append(
            f"""
            <div
              class="pad-wrap"
              data-slot="{html.escape(slot_id, quote=True)}"
              data-folder="{html.escape(folder_label, quote=True)}"
              data-label="{html.escape(label, quote=True)}"
              data-index="{index}"
              data-background-url="{html.escape(background_url, quote=True)}"
            >
              <button
                class="{pad_class}"
                type="button"
                data-slot="{html.escape(slot_id, quote=True)}"
                data-label="{html.escape(label, quote=True)}"
                onclick="playSlotFromButton(this)"
                {style_attr}
                {disabled}
              >
                <strong>{html.escape(label)}</strong>
              </button>
              <button class="favorite-pin" type="button" data-type="pad"
                data-id="{html.escape(slot_id, quote=True)}" aria-pressed="false"
                aria-label="Pin {html.escape(label, quote=True)}" title="Pin favorite"
                onclick="toggleFavorite('pad', this.dataset.id)">☆</button>
            </div>
            """
        )

    if not buttons:
        buttons.append("<p class='empty'>No pads found. Configure the soundboard in K-Sounds Hub first.</p>")

    initial_status = html.escape(status or "Ready")
    voice_fx = state.get("voice_fx", {}) if isinstance(state, dict) else {}
    voice_fx_active = {str(item or "").strip().lower() for item in voice_fx.get("active", [])}
    try:
        voice_fx_amount = max(0, min(100, int(voice_fx.get("amount", 72))))
    except Exception:
        voice_fx_amount = 72
    voice_fx_buttons = []
    for effect_id, effect_label, effect_symbol in VOICE_FX_DEFINITIONS:
        is_active = effect_id in voice_fx_active
        active_class = " active" if is_active else ""
        pressed = "true" if is_active else "false"
        state_text = "Active" if is_active else "Tap to enable"
        voice_fx_buttons.append(
            f'<div class="voice-fx-wrap"><button class="voice-fx-card{active_class}" type="button" data-effect="{html.escape(effect_id, quote=True)}" '
            f'aria-pressed="{pressed}" onclick="toggleVoiceFx(this)">'
            f'<span class="voice-fx-symbol">{html.escape(effect_symbol)}</span>'
            f'<span class="voice-fx-copy"><strong>{html.escape(effect_label)}</strong><small>{state_text}</small></span>'
            '<span class="voice-fx-led" aria-hidden="true"></span></button>'
            f'<button class="favorite-pin" type="button" data-type="voice_fx" data-id="{html.escape(effect_id, quote=True)}" '
            f'aria-pressed="false" aria-label="Pin {html.escape(effect_label, quote=True)}" title="Pin favorite" '
            f'onclick="toggleFavorite(\'voice_fx\', this.dataset.id)">☆</button></div>'
        )
    voice_fx_summary = " + ".join(label for key, label, _symbol in VOICE_FX_DEFINITIONS if key in voice_fx_active) or "Off"

    body = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>K-Sounds Soundboard Remote</title>
      <meta name="theme-color" content="#070a12">
      <meta name="mobile-web-app-capable" content="yes">
      <style>
        :root {{
          color-scheme: dark;
          --bg: #05070d;
          --panel: rgba(7, 11, 20, .72);
          --panel-strong: rgba(10, 16, 29, .88);
          --card: rgba(13, 20, 34, .78);
          --card-hover: rgba(18, 30, 48, .86);
          --bar: rgba(5, 8, 15, .86);
          --cyan: #3ed8ff;
          --cyan-soft: rgba(62, 216, 255, .18);
          --pink: #ff5cc7;
          --pink-soft: rgba(255, 92, 199, .16);
          --text: #ecf7ff;
          --muted: #93a4b8;
          --line: rgba(142, 225, 255, .20);
          --shadow: rgba(0, 0, 0, .48);
        }}
        * {{ box-sizing: border-box; }}
        html, body {{
          overscroll-behavior: none;
          user-select: none;
          -webkit-user-select: none;
        }}
        body {{
          margin: 0;
          min-height: 100vh;
          font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background:
            radial-gradient(circle at 16% -8%, rgba(62,216,255,.30), transparent 34%),
            radial-gradient(circle at 92% 8%, rgba(255,92,199,.22), transparent 30%),
            radial-gradient(circle at 50% 110%, rgba(62,216,255,.10), transparent 38%),
            linear-gradient(135deg, #02040a, #07101d 48%, #05070d);
          color: var(--text);
          padding: 14px 14px 112px;
        }}
        body::before {{
          content: "";
          position: fixed;
          inset: 0;
          pointer-events: none;
          background:
            linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,.018) 1px, transparent 1px);
          background-size: 42px 42px;
          mask-image: linear-gradient(to bottom, rgba(0,0,0,.65), transparent 72%);
        }}
        body.edit-mode header {{
          border-color: rgba(255,92,199,.65);
        }}
        header {{
          position: relative;
          z-index: 20;
          border: 1px solid var(--line);
          border-radius: 24px;
          background:
            linear-gradient(135deg, rgba(10,16,29,.86), rgba(7,11,20,.68)),
            linear-gradient(135deg, rgba(62,216,255,.10), rgba(255,92,199,.08));
          padding: 14px;
          margin-bottom: 14px;
          box-shadow: 0 18px 48px var(--shadow);
          backdrop-filter: blur(18px) saturate(1.35);
        }}
        .brand-row {{
          display: grid;
          grid-template-columns: 54px minmax(0, 1fr) auto;
          gap: 12px;
          align-items: center;
        }}
        .brand-mark {{
          width: 54px;
          height: 54px;
          display: grid;
          place-items: center;
          border: 1px solid rgba(62,216,255,.42);
          border-radius: 18px;
          color: var(--cyan);
          background: rgba(2,4,10,.72);
          font-size: 24px;
          font-weight: 900;
          box-shadow: inset 0 0 18px rgba(62,216,255,.10), 0 12px 28px rgba(0,0,0,.35);
        }}
        .brand-title {{
          min-width: 0;
        }}
        h1 {{
          margin: 0;
          letter-spacing: .11em;
          font-size: 22px;
          line-height: 1.05;
        }}
        .hint, .status {{
          color: var(--muted);
          font-size: 13px;
        }}
        .hint {{
          margin-top: 10px;
        }}
        .status {{
          margin-top: 10px;
          color: var(--cyan);
          min-height: 18px;
          padding: 8px 10px;
          border: 1px solid rgba(62,216,255,.18);
          border-radius: 14px;
          background: rgba(2,4,10,.34);
        }}
        .grid {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax({pad_min_width}px, 1fr));
          gap: var(--ksh-pad-gap, {pad_gap}px);
          position: relative;
          z-index: 1;
        }}
        body.visible-few .grid {{
          grid-template-columns: repeat(auto-fill, minmax(min(142px, 100%), 170px));
          justify-content: start;
        }}
        body.visible-one .grid {{
          grid-template-columns: minmax(min(142px, 100%), 170px);
          justify-content: start;
        }}
        .pad-wrap {{
          position: relative;
          min-width: 0;
          border-radius: var(--ksh-wrap-radius, 22px);
        }}
        .grid,
        .pad-wrap,
        .pad {{
          /* K-Sounds owns pad gestures. Native pan-y used to cancel a held drag in
             Android/WebView as soon as the finger moved. Ordinary one-finger scrolling
             is reproduced in JS until the long-press becomes a drag. */
          touch-action: none;
        }}
        .pad {{ user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }}
        .pad-wrap.selected .pad {{ outline: 3px solid var(--cyan); outline-offset: -3px; }}
        .pad-wrap.selected::after {{ content: "✓"; position: absolute; right: 10px; top: 10px;
          background: var(--cyan); color: #07101b; border-radius: 50%; padding: 2px 7px; pointer-events: none; }}
        .selection-bar {{ position: fixed; z-index: 45; left: 14px;
          right: calc(var(--favorites-width, 168px) + 20px);
          bottom: calc(100px + env(safe-area-inset-bottom, 0px)); display: flex; flex-wrap: wrap;
          align-items: center; gap: 8px; padding: 10px; border: 1px solid #386879;
          border-radius: 16px; background: #0c1928; box-shadow: 0 8px 24px #0008; }}
        .selection-bar[hidden] {{ display: none; }}
        body.edit-mode {{ padding-bottom: 220px; }}
        .selection-bar button, #folderMoveDialog button, #folderMoveDialog select, #folderMoveDialog input {{
          font: inherit; color: #ecf7ff; background: #1b3348; border: 1px solid #46677d; border-radius: 10px; padding: 10px; }}
        .selection-bar button:disabled {{ opacity: .45; }}
        #selectionCount {{ margin-right: auto; }}
        #folderMoveDialog {{ position: fixed; color: #ecf7ff; background: #0c1928;
          border: 1px solid #386879; border-radius: 18px; width: min(440px, calc(100% - 48px));
          max-height: 80vh; overflow: auto; padding: 20px; }}
        #folderMoveDialog::backdrop {{ background: #000a; }}
        #folderMoveDialog label, #folderMoveDialog select, #folderMoveDialog input {{ display: block;
          box-sizing: border-box; width: 100%; margin: 12px 0; }}
        #folderMoveDialog [hidden] {{ display: none; }}
        #moveError {{ color: #ffabb7; min-height: 1.5em; }}
        .drag-ghost {{ position: fixed; top: 0; left: 0; z-index: 90; pointer-events: none;
          opacity: .94; transform: translate(-200vw, -200vh); will-change: transform; }}
        .remote-version {{ font-size: 12px; color: #93a4b8; margin-top: 6px; }}
        .edit-guide {{ flex-basis: 100%; font-size: 13px; color: #a9c1d2; }}
        body.edit-mode .pad-wrap::before {{ content: ""; position: absolute; top: 10px; right: 10px;
          z-index: 4; width: 22px; height: 22px; border: 2px solid #abc8dc; border-radius: 6px;
          background: #07101bbb; pointer-events: none; }}
        body.edit-mode .pad-wrap.selected::before {{ border-color: #3ed8ff; background: #3ed8ff; }}
        body.edit-mode .pad-wrap.selected::after {{ z-index: 5; border-radius: 4px; padding: 1px 5px;
          right: 12px; top: 11px; font: bold 18px sans-serif; }}
        .pad-wrap.drag-source .pad {{ outline: 3px solid #3ed8ff; opacity: .35; }}
        .pad-wrap.swap-target .pad {{ outline: 4px solid #ff5cc7; }}
        .pad-wrap.swap-target::after {{ content: "Swap"; position: absolute; z-index: 8; left: 8px;
          top: 8px; padding: 4px 8px; border-radius: 8px; color: #100918; background: #ff5cc7; }}
        .drop-marker {{ position: fixed; z-index: 75; border-radius: 3px; background: #3ed8ff;
          box-shadow: 0 0 12px #3ed8ff99; pointer-events: none; }}
        .drag-ghost .pad {{ min-height: 0; padding: 10px; border: 2px solid #3ed8ff; backdrop-filter: none;
          box-shadow: 0 8px 25px #0008; }}
        .drag-ghost .pad strong {{ font-size: 14px; overflow: hidden; text-overflow: ellipsis; }}
        .pad {{
          position: relative;
          overflow: hidden;
          width: 100%;
          min-height: var(--ksh-pad-height, {pad_min_height}px);
          aspect-ratio: 1 / 0.84;
          border: 1px solid rgba(62,216,255,.28);
          border-radius: var(--ksh-pad-radius, {pad_radius}px);
          background:
            linear-gradient(135deg, rgba(255,255,255,.10), rgba(3,7,13,.30)),
            linear-gradient(135deg, var(--pad-auto-a, #18487e), var(--pad-auto-b, #5a2d8e));
          background-size: cover;
          background-position: center;
          background-repeat: no-repeat;
          color: var(--text);
          padding: var(--ksh-pad-padding, {pad_padding}px);
          text-align: left;
          box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 14px 34px rgba(0,0,0,.30);
          touch-action: none;
          backdrop-filter: none; /* Keep pad rendering inexpensive. */
        }}
        .pad::before {{
          content: "";
          position: absolute;
          inset: 0;
          pointer-events: none;
          background: linear-gradient(135deg, rgba(255,255,255,.08), transparent 34%, rgba(62,216,255,.05));
          opacity: .70;
        }}
        .pad > * {{
          position: relative;
          z-index: 1;
        }}
        .pad.has-bg {{
          background-image:
            linear-gradient(135deg, rgba(5, 8, 15, .30), rgba(5, 8, 15, .76)),
            var(--pad-bg);
        }}
        .pad.has-bg span {{
          color: rgba(236, 247, 255, .82);
          text-shadow: 0 2px 8px rgba(0,0,0,.85);
        }}
        .pad.has-bg strong {{
          text-shadow: 0 2px 10px rgba(0,0,0,.92);
        }}
        .pad:active, .pad.sending {{
          transform: scale(.98);
          border-color: rgba(255,92,199,.88);
          background-color: var(--card-hover);
          box-shadow: inset 0 0 0 1px rgba(255,92,199,.22), 0 10px 28px rgba(255,92,199,.12);
        }}
        body.edit-mode .pad {{
          border-color: rgba(255,92,199,.58);
          cursor: grab;
        }}
        .pad:disabled {{
          opacity: .38;
        }}
        .pad strong {{
          display: block;
          font-size: var(--ksh-title-font, {pad_title_font}px);
          margin-bottom: var(--ksh-title-margin, {pad_title_margin}px);
        }}
        .pad span {{
          display: block;
          color: var(--muted);
          font-size: var(--ksh-subtitle-font, {pad_subtitle_font}px);
          word-break: break-word;
        }}
        .pad-wrap.drag-source .pad {{
          opacity: .58;
          border-color: rgba(255,92,199,1);
          box-shadow: 0 0 0 2px rgba(255,92,199,.20), 0 14px 34px rgba(0,0,0,.28);
          transform: scale(.98);
        }}

        .pad-wrap.drop-target .pad {{
          border-color: rgba(62,216,255,1);
          box-shadow: 0 0 0 2px rgba(62,216,255,.24), 0 14px 34px rgba(0,0,0,.28);
          transform: scale(1.01);
        }}
        .pad-wrap.drag-source::after,
        .pad-wrap.drop-target::after {{
          position: absolute;
          top: 8px;
          right: 8px;
          z-index: 4;
          min-width: 52px;
          padding: 4px 8px;
          border-radius: 999px;
          font-size: 10px;
          font-weight: 900;
          letter-spacing: .08em;
          text-align: center;
          box-shadow: 0 10px 20px rgba(0,0,0,.30);
        }}
        .pad-wrap.drag-source::after {{
          content: "SOURCE";
          color: #071018;
          background: rgba(255,92,199,.96);
        }}
        .pad-wrap.drop-target::after {{
          content: "TARGET";
          color: #071018;
          background: rgba(62,216,255,.96);
        }}
        .empty {{
          color: var(--muted);
        }}
        .bottom-bar {{
          position: fixed;
          left: 10px;
          /* Give the full-height favorite rail its own footprint, including below the pads. */
          right: calc(var(--favorites-width, 168px) + 20px);
          bottom: 10px;
          z-index: 50;
          display: grid;
          grid-template-columns: 46px 46px minmax(0, 1fr) 46px;
          gap: 7px;
          align-items: center;
          padding: 7px 9px calc(7px + env(safe-area-inset-bottom));
          border: 1px solid rgba(62,216,255,.20);
          border-radius: 24px;
          background: var(--bar);
          backdrop-filter: blur(20px) saturate(1.35);
          box-shadow: 0 -10px 42px rgba(0,0,0,.44);
        }}
        .bottom-volume {{
          min-width: 0;
        }}
        .volume-line {{
          display: grid;
          grid-template-columns: auto 1fr auto;
          gap: 10px;
          align-items: center;
        }}
        .vol-icon {{
          font-size: 18px;
          line-height: 1;
        }}
        input[type="range"] {{
          width: 100%;
          accent-color: var(--cyan);
        }}
        #globalVolValue {{
          min-width: 42px;
          text-align: right;
          color: var(--cyan);
          font-weight: 800;
          font-size: 13px;
        }}
        .tiny {{
          color: var(--muted);
          font-size: 11px;
          margin-top: 4px;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }}
        .edit-mini, .options-mini, .stop-mini {{
          width: 46px;
          height: 46px;
          border-radius: 15px;
          color: var(--text);
          font-size: 22px;
          line-height: 1;
          font-weight: 900;
          box-shadow: 0 12px 28px rgba(0,0,0,.32);
          touch-action: manipulation;
        }}
        .edit-mini {{
          border: 1px solid rgba(62,216,255,.62);
          background: rgba(9, 26, 35, .92);
        }}
        body.edit-mode .edit-mini {{
          border-color: rgba(255,92,199,.95);
          background: rgba(35, 9, 28, .92);
        }}
        .stop-mini {{
          border: 1px solid rgba(255,92,199,.62);
          background: rgba(35, 9, 28, .92);
          font-size: 24px;
        }}
        .edit-mini:active, .options-mini:active, .stop-mini:active {{
          transform: scale(.96);
        }}

        .disconnect-link {{
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-height: 42px;
          padding: 0 14px;
          border: 1px solid rgba(255, 92, 199, 0.58);
          border-radius: 999px;
          color: #ffe1f0;
          background: rgba(36, 14, 32, 0.72);
          text-decoration: none;
          font-weight: 800;
          letter-spacing: 0.02em;
          box-shadow: 0 10px 22px rgba(0,0,0,.28);
        }}
        .disconnect-link:active {{
          transform: scale(0.98);
        }}
        .remote-subtitle {{
          margin: 4px 0 0;
          color: rgba(236, 247, 255, 0.70);
          font-size: 0.76rem;
          letter-spacing: 0.12em;
          text-transform: uppercase;
        }}
        .module-switch {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:12px; padding:4px; border:1px solid rgba(142,225,255,.14); border-radius:16px; background:rgba(2,4,10,.32); }}
        .module-switch button {{ min-height:42px; border:1px solid transparent; border-radius:12px; color:var(--muted); background:transparent; font:inherit; font-weight:800; letter-spacing:.02em; }}
        .module-switch button.active {{ color:var(--text); border-color:rgba(62,216,255,.42); background:linear-gradient(135deg,rgba(62,216,255,.16),rgba(255,92,199,.08)); box-shadow:inset 0 0 16px rgba(62,216,255,.06); }}
        .remote-module[hidden] {{ display:none !important; }}
        body.remote-mode-voice {{ padding-bottom:24px; }}
        body.remote-mode-voice .bottom-bar {{ display:none; }}
        body.remote-mode-voice .selection-bar {{ display:none !important; }}
        .voice-fx-module {{ position:relative; z-index:1; display:grid; gap:12px; }}
        .voice-fx-overview {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; padding:14px; border:1px solid rgba(62,216,255,.22); border-radius:20px; background:linear-gradient(135deg,rgba(10,16,29,.82),rgba(8,13,23,.70)); box-shadow:0 14px 34px rgba(0,0,0,.28); }}
        .voice-fx-overview strong {{ display:block; font-size:16px; }}
        .voice-fx-overview small {{ display:block; margin-top:4px; color:var(--muted); }}
        .voice-fx-strength {{ min-width:74px; padding:7px 10px; border-radius:999px; color:var(--cyan); text-align:center; font-size:12px; font-weight:850; border:1px solid rgba(62,216,255,.24); background:rgba(62,216,255,.08); }}
        .voice-fx-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
        .voice-fx-card {{ min-width:0; min-height:92px; display:grid; grid-template-columns:38px minmax(0,1fr) 10px; gap:10px; align-items:center; padding:12px; border:1px solid rgba(142,225,255,.16); border-radius:18px; color:var(--text); background:linear-gradient(135deg,rgba(13,20,34,.82),rgba(8,13,24,.72)); text-align:left; font:inherit; box-shadow:inset 0 1px 0 rgba(255,255,255,.035),0 12px 26px rgba(0,0,0,.25); touch-action:manipulation; }}
        .voice-fx-card:active {{ transform:scale(.985); }}
        .voice-fx-card.active {{ border-color:rgba(62,216,255,.72); background:linear-gradient(135deg,rgba(20,63,82,.82),rgba(45,22,63,.72)); box-shadow:inset 0 0 20px rgba(62,216,255,.08),0 0 22px rgba(62,216,255,.08); }}
        .voice-fx-card.sending {{ opacity:.62; }}
        .voice-fx-symbol {{ width:38px; height:38px; display:grid; place-items:center; border-radius:13px; color:var(--cyan); font-size:22px; font-weight:900; border:1px solid rgba(62,216,255,.26); background:rgba(2,4,10,.40); }}
        .voice-fx-copy {{ min-width:0; }}
        .voice-fx-copy strong {{ display:block; font-size:14px; overflow:hidden; text-overflow:ellipsis; }}
        .voice-fx-copy small {{ display:block; margin-top:4px; color:var(--muted); font-size:11px; }}
        .voice-fx-card.active .voice-fx-copy small {{ color:rgba(190,239,255,.88); }}
        .voice-fx-led {{ width:9px; height:9px; border-radius:50%; background:rgba(147,164,184,.36); }}
        .voice-fx-card.active .voice-fx-led {{ background:var(--cyan); box-shadow:0 0 12px rgba(62,216,255,.78); }}
        @media (max-width:390px) {{ .voice-fx-grid {{ grid-template-columns:1fr; }} .voice-fx-card {{ min-height:76px; }} }}

        /* IP15b: persistent, independent right-hand favorites workspace. */
        body {{ padding-right: calc(var(--favorites-width, 168px) + 20px); }}
        .favorite-rail {{ position:fixed; top:14px; right:8px; bottom:calc(8px + env(safe-area-inset-bottom, 0px));
          z-index:35; display:flex; flex-direction:column; gap:7px; width:var(--favorites-width, 168px);
          min-height:0; padding:9px; border:1px solid rgba(62,216,255,.25); border-radius:18px;
          background:rgba(5,10,18,.95); box-shadow:0 8px 24px rgba(0,0,0,.32); }}
        .favorite-headline {{ display:flex; align-items:center; justify-content:space-between; gap:5px;
          min-width:0; flex-shrink:0; font-size:11px; letter-spacing:.07em; color:var(--cyan); font-weight:800; }}
        .favorite-headline small {{ font-size:11px; color:var(--muted); letter-spacing:0; }}
        .favorite-rail button {{ font:inherit; color:var(--text); border:1px solid rgba(142,225,255,.20);
          border-radius:10px; background:rgba(20,31,49,.86); cursor:pointer; touch-action:manipulation; }}
        .favorite-rail .favorite-edit, .favorite-rail #floatingFavoritesButton {{ flex-shrink:0; width:30px;
          height:30px; min-height:30px; display:grid; place-items:center; text-decoration:none;
          border:1px solid rgba(142,225,255,.20); border-radius:10px; color:var(--text); }}
        .favorite-rail #floatingFavoritesButton[hidden] {{ display:none; }}
        .favorite-actions {{ display:grid; align-content:start; grid-template-columns:repeat(var(--favorites-columns,1),minmax(0,1fr));
          gap:var(--favorites-gap,6px); overflow-y:auto; overflow-x:hidden; min-height:0; flex:1;
          overscroll-behavior:contain; -webkit-overflow-scrolling:touch; touch-action:pan-y;
          scrollbar-width:thin; scrollbar-color:rgba(62,216,255,.50) transparent; }}
        .favorite-actions .favorite-tile {{ position:relative; min-height:var(--favorites-tile-height,82px); padding:8px 5px;
          display:flex; flex-direction:column; align-items:flex-start; justify-content:flex-end; min-width:0;
          overflow:hidden; text-align:left; background:linear-gradient(135deg,var(--favorite-color-a,#17324a),var(--favorite-color-b,#251c3c));
          background-image:linear-gradient(135deg,rgba(5,8,15,.26),rgba(5,8,15,.75)),var(--favorite-background,none);
          background-size:cover; background-position:center; }}
        .favorite-tile .favorite-kind {{ position:absolute; top:5px; left:5px; border:1px solid rgba(236,247,255,.24);
          border-radius:5px; background:rgba(3,7,14,.65); padding:2px 4px; font-size:9px; color:var(--cyan); }}
        .favorite-tile .favorite-label {{ display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
          overflow:hidden; overflow-wrap:anywhere; font-size:clamp(10px,1.9vw,13px); font-weight:750; line-height:1.2;
          text-shadow:0 1px 5px #000b; max-width:100%; }}
        .favorite-tile.active {{ border-color:var(--cyan); box-shadow:inset 0 0 0 1px var(--cyan); }}
        .favorite-tile:disabled {{ opacity:.45; }}
        .favorite-empty {{ grid-column:1/-1; color:var(--muted); font-size:12px; line-height:1.5; padding:6px; }}
        /* Small proportional stars: preserve the artwork; size follows each pad's own width. */
        .pad-wrap, .voice-fx-wrap {{ container-type:inline-size; }}
        .favorite-pin {{ position:absolute; z-index:12; right:5px; top:5px;
          width:23px; height:23px; padding:0;
          display:grid; place-items:center; border:1px solid rgba(236,247,255,.24); border-radius:50%;
          color:var(--muted); background:rgba(4,9,17,.66); font:700 14px system-ui; line-height:1;
          touch-action:manipulation; cursor:pointer; -webkit-tap-highlight-color:transparent; }}
        @supports (width: 1cqw) {{
          .favorite-pin {{ width:clamp(19px,14cqw,25px); height:clamp(19px,14cqw,25px);
            font-size:clamp(12px,9cqw,16px);
            right:clamp(3px,3cqw,6px); top:clamp(3px,3cqw,6px); }}
        }}
        .favorite-pin::before {{ content:""; position:absolute; inset:-3px; border-radius:50%; }}
        .favorite-pin[aria-pressed="true"] {{ color:#ffdf79; border-color:rgba(255,223,121,.65); }}
        .favorite-pin:disabled {{ opacity:.4; }}
        body.edit-mode .pad-wrap > .favorite-pin {{ top:auto; bottom:5px; }}
        .voice-fx-wrap {{ position:relative; min-width:0; }}
        .voice-fx-wrap > .voice-fx-card {{ width:100%; height:100%; padding-right:29px; }}
        .favorite-settings-title {{ grid-column:1/-1; color:var(--cyan); font-weight:800; padding-top:8px; }}
        .favorite-setting output {{ display:block; color:var(--cyan); font-size:12px; margin-top:3px; }}
        .favorite-setting input[type="range"] {{ width:100%; min-width:0; }}
        .favorite-setting select {{ width:100%; }}
        @media (max-width:520px) {{
          .favorite-rail {{ right:5px; padding:5px; }}
          .favorite-actions .favorite-tile {{ padding:5px 3px; }}
          .bottom-bar {{ grid-template-columns:38px 38px minmax(0,1fr) 38px;
            gap:4px; padding:6px 6px calc(6px + env(safe-area-inset-bottom));
            border-radius:17px; }}
          .edit-mini, .options-mini, .stop-mini {{ width:38px; height:38px;
            border-radius:12px; font-size:18px; }}
          .volume-line {{ grid-template-columns:minmax(0,1fr) auto; gap:4px; }}
          .volume-line .vol-icon {{ display:none; }}
          #globalVolValue {{ min-width:29px; font-size:11px; }}
        }}
        .favorite-editor {{ position:fixed; inset:12% 12px 12%; z-index:100; overflow:auto;
          border:1px solid rgba(62,216,255,.5); border-radius:19px; padding:16px;
          color:var(--text); background:#0b1422; box-shadow:0 0 0 100vmax rgba(0,0,0,.7); }}
        .favorite-editor[hidden] {{ display:none; }}
        .favorite-editor button {{ font:inherit; color:var(--text); border:1px solid rgba(142,225,255,.22);
          border-radius:10px; padding:8px; background:rgba(20,31,49,.85); }}
        .favorite-editor .favorite-entry {{ display:flex; align-items:center; justify-content:space-between;
          gap:12px; width:100%; margin:5px 0; text-align:left; }}
        .favorite-editor .favorite-entry[aria-pressed="true"] {{ border-color:var(--cyan); }}
        .favorite-editor .favorite-head {{ display:flex; justify-content:space-between; align-items:center; }}
        .favorite-editor h3 {{ margin:16px 0 8px; }}
        .soundboard-content {{
          position: relative;
          z-index: 1;
        }}

        .folder-bar {{
          display: flex;
          gap: 9px;
          overflow-x: auto;
          padding: 8px 4px;
          margin: 2px 0 12px;
          scrollbar-width: thin;
          position: sticky;
          top: env(safe-area-inset-top, 0px);
          z-index: 30;
          background: rgba(5, 10, 18, .96);
          border-bottom: 1px solid var(--line);
          border-radius: 14px;
          align-items: center;
        }}
        .folder-settings {{
          flex: 0 0 auto;
          min-height: 44px;
          padding: 8px 12px;
          color: var(--cyan);
          background: rgba(14, 42, 62, .92);
          border: 1px solid rgba(62,216,255,.4);
          border-radius: 12px;
          font-weight: 700;
          white-space: nowrap;
          order: -1;
          touch-action: manipulation;
        }}
        .folder-pill {{
          min-height: 40px;
          border: 1px solid rgba(142,225,255,0.22);
          background:
            linear-gradient(135deg, rgba(255,255,255,.075), rgba(255,255,255,.035)),
            rgba(5, 10, 18, .58);
          color: rgba(236,247,255,0.90);
          border-radius: 999px;
          padding: 9px 15px;
          font-size: 13px;
          font-weight: 850;
          letter-spacing: .015em;
          white-space: nowrap;
          box-shadow: inset 0 1px 0 rgba(255,255,255,.09), 0 10px 24px rgba(0,0,0,.18);
          backdrop-filter: blur(12px) saturate(1.2);
        }}
        .folder-pill.active {{
          border-color: rgba(106,214,255,0.78);
          background:
            linear-gradient(135deg, rgba(62,216,255,.34), rgba(184,92,255,.22)),
            rgba(5, 12, 22, .74);
          color: #ffffff;
          box-shadow: inset 0 1px 0 rgba(255,255,255,.14), 0 12px 30px rgba(62,216,255,.12);
        }}
        .pad-wrap[hidden] {{
          display: none !important;
        }}

        body.contrast-high {{
          --text: #ffffff;
          --muted: rgba(225, 238, 255, .86);
          --line: rgba(142, 225, 255, .38);
          --card: rgba(8, 14, 26, .90);
          --card-hover: rgba(16, 32, 54, .94);
        }}
        body.contrast-soft {{
          --muted: rgba(204, 218, 235, .62);
          --line: rgba(142, 225, 255, .14);
          --card: rgba(13, 20, 34, .62);
          --card-hover: rgba(18, 30, 48, .70);
        }}
        body.remote-bg-clean {{
          background: linear-gradient(135deg, #040711, #07101d 52%, #040711);
        }}
        body.remote-bg-clean::before {{
          display: none;
        }}
        body.remote-bg-grid {{
          background:
            radial-gradient(circle at 16% -8%, rgba(62,216,255,.24), transparent 34%),
            radial-gradient(circle at 92% 8%, rgba(255,92,199,.18), transparent 30%),
            linear-gradient(135deg, #02040a, #07101d 48%, #05070d);
        }}
        body.remote-bg-dark {{
          background: #02040a;
        }}
        body.remote-bg-dark::before {{
          display: none;
        }}

        .options-panel {{
          position: fixed;
          left: 14px;
          right: 14px;
          bottom: calc(106px + env(safe-area-inset-bottom, 0px));
          max-width: 680px;
          max-height: calc(100vh - 140px);
          overflow-y: auto;
          z-index: 80;
          border: 1px solid rgba(142, 225, 255, .18);
          border-radius: 24px;
          padding: 13px;
          margin: 0 0 14px;
          background:
            linear-gradient(135deg, rgba(10,16,29,.98), rgba(7,11,20,.98)),
            radial-gradient(circle at top left, rgba(62,216,255,.13), transparent 38%),
            radial-gradient(circle at top right, rgba(255,92,199,.10), transparent 34%);
          box-shadow: 0 18px 48px rgba(0,0,0,.30);
          backdrop-filter: blur(18px) saturate(1.25);
        }}
        .options-title {{
          font-size: 12px;
          font-weight: 900;
          letter-spacing: .14em;
          text-transform: uppercase;
          color: var(--cyan);
          margin-bottom: 10px;
        }}
        .options-heading {{ display: flex; justify-content: space-between; align-items: center; }}
        .options-close {{
          min-height: 44px;
          padding: 6px 14px;
          color: var(--text);
          background: rgba(255,255,255,.08);
          border: 1px solid var(--line);
          border-radius: 12px;
        }}
        .options-grid {{
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(138px, 1fr));
          gap: 10px;
        }}
        .options-grid label {{
          display: grid;
          gap: 5px;
          color: var(--muted);
          font-size: 11px;
          font-weight: 800;
          letter-spacing: .06em;
          text-transform: uppercase;
        }}
        .options-grid select {{
          width: 100%;
          min-height: 42px;
          border: 1px solid rgba(142, 225, 255, .22);
          border-radius: 14px;
          color: var(--text);
          background: rgba(2, 6, 12, .86);
          padding: 0 10px;
          font-weight: 800;
          outline: none;
        }}
        .options-grid select:focus {{
          border-color: rgba(62,216,255,.70);
          box-shadow: 0 0 0 2px rgba(62,216,255,.12);
        }}
        .options-grid input[type="checkbox"] {{
          width: 22px;
          height: 22px;
          margin: 2px 0 0;
          accent-color: var(--cyan);
          justify-self: start;
        }}

        .options-mini {{
          border: 1px solid rgba(142,225,255,.42);
          background: rgba(9, 26, 35, .78);
        }}
        body.options-open .options-mini {{
          border-color: rgba(62,216,255,.95);
          background: rgba(14, 42, 62, .92);
        }}

        body.folder-size-large .folder-pill {{
          min-height: 48px;
          padding: 11px 18px;
          font-size: 15px;
        }}
        body.folder-size-xl .folder-pill {{
          min-height: 54px;
          padding: 12px 22px;
          font-size: 16px;
          letter-spacing: .025em;
        }}
        body.folder-style-tabs .folder-bar {{
          gap: 8px;
          padding: 8px 4px;
          border-bottom: 1px solid rgba(142,225,255,.10);
        }}
        body.folder-style-tabs .folder-pill {{
          border-radius: 15px;
          background:
            linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.025)),
            rgba(5, 10, 18, .60);
        }}
        body.folder-style-tabs .folder-pill.active {{
          border-color: rgba(62,216,255,.82);
          box-shadow: inset 0 -2px 0 rgba(62,216,255,.72), 0 12px 30px rgba(62,216,255,.10);
        }}
        body.folder-style-drawer .soundboard-content {{
          display: grid;
          grid-template-columns: minmax(128px, 188px) minmax(0, 1fr);
          gap: 13px;
          align-items: start;
        }}
        body.folder-style-drawer .folder-bar {{
          position: sticky;
          top: env(safe-area-inset-top, 0px);
          display: flex;
          flex-direction: column;
          align-items: stretch;
          max-height: calc(100vh - 142px);
          overflow-x: hidden;
          overflow-y: auto;
          padding: 5px;
          margin: 0;
          border: 1px solid rgba(142,225,255,.14);
          border-radius: 18px;
          background: rgba(5, 10, 18, .96);
          contain: layout paint;
        }}
        body.folder-style-drawer .folder-pill {{
          width: 100%;
          min-width: 0;
          text-align: left;
          border-radius: 13px;
          overflow: hidden;
          text-overflow: ellipsis;
        }}
        @media (max-width: 520px) {{
          body.folder-style-drawer .soundboard-content {{
            grid-template-columns: minmax(104px, 32vw) minmax(0, 1fr);
            gap: 8px;
          }}
          body.folder-style-drawer .folder-pill {{
            padding-left: 10px;
            padding-right: 10px;
          }}
        }}

</style>
    </head>
    <body class="{html.escape(body_classes, quote=True)}">
      <header>
        <div class="brand-row">
          <div class="brand-mark">K</div>
          <div class="brand-title">
            <h1>K-SOUNDS</h1>
            <p class="remote-subtitle">{html.escape(remote_subtitle)}</p>
          </div>
          <a class="disconnect-link" href="ksounds://disconnect">Disconnect</a>
        </div>
        <div id="remoteGestureHint" class="hint">{html.escape(initial_gesture_hint)}</div>
        <div id="remoteStatus" class="status">{initial_status}</div>
        <div class="remote-version">Remote IP10t · Hub IP14n10c · Favorites IP15c · <span id="apkVersion">Browser / older APK</span></div>
        {module_switch_html}
      </header>

      <section id="remoteOptionsPanel" class="options-panel" aria-label="Remote options" hidden>
        <div class="options-heading">
          <div class="options-title">Remote Options</div>
          <button class="options-close" type="button" onclick="toggleOptionsPanel(false)">Done</button>
        </div>
        <div class="options-grid">
          <label>
            <span>Contrast</span>
            <select onchange="saveRemoteOption('remote_contrast', this.value)">
              <option value="normal"{option_selected(remote_contrast, "normal")}>Normal</option>
              <option value="high"{option_selected(remote_contrast, "high")}>High</option>
              <option value="soft"{option_selected(remote_contrast, "soft")}>Soft</option>
            </select>
          </label>
          <label>
            <span>Background</span>
            <select onchange="saveRemoteOption('remote_background', this.value)">
              <option value="glass"{option_selected(remote_background, "glass")}>Glass</option>
              <option value="clean"{option_selected(remote_background, "clean")}>Clean</option>
              <option value="grid"{option_selected(remote_background, "grid")}>Grid</option>
              <option value="dark"{option_selected(remote_background, "dark")}>Dark</option>
            </select>
          </label>
          <label>
            <span>Folders</span>
            <select onchange="saveRemoteOption('remote_folder_size', this.value)">
              <option value="normal"{option_selected(remote_folder_size, "normal")}>Normal</option>
              <option value="large"{option_selected(remote_folder_size, "large")}>Large</option>
              <option value="xl"{option_selected(remote_folder_size, "xl")}>XL</option>
            </select>
          </label>
          <label>
            <span>Folder style</span>
            <select id="folderStyleSelect" onchange="saveRemoteOption('remote_folder_style', this.value)">
              <option value="pills"{option_selected(remote_folder_style, "pills")}>Pills</option>
              <option value="tabs"{option_selected(remote_folder_style, "tabs")}>Tabs</option>
              <option value="drawer"{option_selected(remote_folder_style, "drawer")}>Side drawer</option>
            </select>
          </label>
          <div class="favorite-settings-title">Favorites · right sidebar</div>
          <label class="favorite-setting">
            <span>Sidebar width</span>
            <input id="favoritesWidthOption" type="range" min="120" max="340" step="10"
              oninput="setFavoriteLayout('width', this.value)">
            <output id="favoritesWidthValue">168 px</output>
          </label>
          <label class="favorite-setting">
            <span>Favorites per row</span>
            <select id="favoritesColumnsOption" onchange="setFavoriteLayout('columns', this.value)">
              <option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="4">4</option>
            </select>
          </label>
          <label class="favorite-setting">
            <span>Favorite pad height</span>
            <input id="favoritesHeightOption" type="range" min="52" max="140" step="4"
              oninput="setFavoriteLayout('height', this.value)">
            <output id="favoritesHeightValue">82 px</output>
          </label>
          <label class="favorite-setting">
            <span>Main pads per row</span>
            <input id="padsColumnsOption" type="range" min="1" max="13" step="1"
              oninput="applyColumns(this.value, true)">
            <output id="padsColumnsValue">4</output>
          </label>
          <label>
            <span>Resize outside Edit</span>
            <input id="pinchOutsideEditOption" type="checkbox"
              onchange="saveRemoteOption('remote_pinch_outside_edit', this.checked ? 'true' : 'false')"
              {"checked" if remote_pinch_outside_edit else ""}>
          </label>
        </div>
        <p class="hint">Side drawer places folders in a vertical panel on the left.</p>
        <p class="hint">Display: follows the device refresh rate. Rendering has no fixed app frame cap.</p>
        <p id="nativeAppOptions" hidden><a href="ksounds://app-options">App updates &amp; display…</a></p>
        <label id="floatingAutoRow" hidden>
          <span>Auto-show Android edge favorites outside K-Sounds</span>
          <input id="floatingAutoOption" type="checkbox" onchange="setFloatingAuto(this.checked)">
        </label>
        <p class="hint" id="floatingAutoHint" hidden>When enabled, the side handle appears after you leave K-Sounds Remote and disappears when you return. Android overlay permission is required.</p>
      </section>

      <aside id="favoritesRail" class="favorite-rail" aria-label="Quick favorites">
        <div class="favorite-headline"><span>★ FAVORITES</span><small id="favoritesCount">0</small>
          <a id="floatingFavoritesButton" href="ksounds://floating-toggle" hidden aria-hidden="true"
             aria-label="Legacy manual detach control">↗</a>
          <button class="favorite-edit" type="button" onclick="openFavoriteEditor()" aria-label="Manage favorites" title="Manage favorites">✎</button></div>
        <div id="favoritesActions" class="favorite-actions"></div>
      </aside>
      <section id="favoriteEditor" class="favorite-editor" aria-label="Edit quick favorites" hidden>
        <div class="favorite-head"><strong>Quick favorites</strong><button type="button" onclick="closeFavoriteEditor()">Done</button></div>
        <p class="hint">Pin pads and Voice FX. Pads and Voice FX, synchronized through K-Sounds on this PC.</p>
        <div id="favoriteChoices"></div>
      </section>

      <section id="padsModule" class="soundboard-content remote-module"{pads_hidden_attr}>
        {folder_bar_html}
        <main id="padsGrid" class="grid">{''.join(buttons)}</main>
      </section>

      <section id="voiceFxModule" class="voice-fx-module remote-module"{voice_hidden_attr} aria-label="Voice FX controls">
        <div class="voice-fx-overview">
          <div><strong>MICRO Voice FX</strong><small id="voiceFxSummary">{html.escape(voice_fx_summary)}</small></div>
          <div id="voiceFxStrength" class="voice-fx-strength">Strength {voice_fx_amount}%</div>
        </div>
        <div id="voiceFxGrid" class="voice-fx-grid">{''.join(voice_fx_buttons)}</div>
      </section>

      <section id="selectionBar" class="selection-bar" hidden aria-label="Selected sounds">
        <div class="edit-guide">Tap pads to select them. Select exactly two to swap their positions. All keeps every sound.</div>
        <span id="selectionCount" aria-live="polite">0 selected</span>
        <button type="button" onclick="selectVisibleSounds()">Select visible</button>
        <button type="button" onclick="clearSoundSelection()">Clear</button>
        <button id="swapSelectedButton" type="button" onclick="swapSelectedSounds()" disabled>Swap 2 selected</button>
        <button id="moveSelectedButton" type="button" onclick="openMoveFolderDialog()" disabled>Add / move to folder…</button>
        <button id="removeFolderButton" type="button" onclick="removeSelectedFromFolder()" hidden>Remove from folder</button>
        <button type="button" onclick="setEditMode(false)">Done</button>
      </section>
      <dialog id="folderMoveDialog" aria-labelledby="moveTitle">
        <h2 id="moveTitle">Move selected sounds</h2>
        <label>Destination folder<select id="moveFolderSelect" onchange="updateMoveDestination()"></select></label>
        <label id="newFolderLabel" hidden>New folder name<input id="newFolderName" maxlength="32" autocomplete="off"></label>
        <p id="moveError" role="status"></p>
        <button id="confirmFolderMove" type="button" onclick="moveSelectedToFolder()">Move sounds</button>
        <button type="button" onclick="closeMoveFolderDialog()">Cancel</button>
      </dialog>

      <section class="bottom-bar" aria-label="Soundboard controls">
        <button id="editButton" class="edit-mini" type="button" onclick="toggleEditMode()" title="Edit" aria-label="Edit">✎</button>
        <button id="optionsButton" class="options-mini" type="button" onclick="toggleOptionsPanel()" title="Options" aria-label="Options">⚙</button>
        <div class="bottom-volume">
          <div class="volume-line">
            <span class="vol-icon">🔊</span>
            <input
              id="globalVolumeSlider"
              type="range"
              min="0"
              max="100"
              name="volume"
              value="{global_volume}"
              oninput="scheduleVolumeUpdate(this.value)"
              onchange="sendVolumeNow(this.value)"
            >
            <span id="globalVolValue">{global_volume}%</span>
          </div>
          <span id="volumeSendState" style="display:none">Ready</span>
        </div>
        <button class="stop-mini" type="button" onclick="stopAllSounds()" title="Stop all" aria-label="Stop all">■</button>
      </section>

      <script>
        const apkMatch = navigator.userAgent.match(/KSHRemote[/]([A-Za-z0-9][A-Za-z0-9._-]*)/);
        document.getElementById("nativeAppOptions").hidden = !apkMatch;
        document.getElementById("apkVersion").textContent = apkMatch ? "APK " + apkMatch[1] : "Browser / older APK";
        function kshSetAutoFloating(enabled) {{
          const check = document.getElementById("floatingAutoOption");
          if (check) check.checked = !!enabled;
        }}
        function setFloatingAuto(enabled) {{
          window.location.href = enabled ? "ksounds://floating-auto-on" : "ksounds://floating-auto-off";
        }}
        let volumeTimer = null;
        let lastSentVolume = "{global_volume}";
        let volumeDragging = false;
        let editMode = window.sessionStorage.getItem("ksoundSoundboardEditMode") === "1";
        const remoteModuleAvailability = {{ pads: {"true" if pads_available else "false"}, voice: {"true" if voice_fx_available else "false"} }};
        const defaultRemoteMode = "{default_remote_mode}";
        let remoteMode = window.localStorage.getItem("ksoundRemoteModule") || defaultRemoteMode;
        if (!remoteModuleAvailability[remoteMode]) remoteMode = defaultRemoteMode;
        const voiceFxPending = new Set();
        let allowPinchOutsideEdit = {"true" if remote_pinch_outside_edit else "false"};
        let lastEditToggleAt = 0;
        let dragState = null;
        const selectedSoundIds = new Set();
        let selectionMovePending = false;
        let longPressTimer = 0;
        let dragFrame = 0;
        const LONG_PRESS_MS = 400;
        const SCROLL_INTENT_PX = 12;
        const EDIT_HINT = "Edit: tap to select · Hold and drag to insert · Pinch to resize";
        const PLAY_HINT = "Tap to play · Hold and drag onto a pad to swap";
        function playHint() {{
          return allowPinchOutsideEdit ? PLAY_HINT + " · Pinch to resize" : PLAY_HINT;
        }}
        let orderSavePending = false;
        let activePointers = new Map();
        let pinchState = null;
        const GRID_MIN_COLUMNS = 1;
        const GRID_MAX_COLUMNS = 13;
        const PINCH_RATIO_PER_STEP = 1.08;
        let gestureCooldownUntil = 0;
        let layoutSaveTimer = null;
        let serverColumns = Number("{remote_columns}");
        let storedColumns = Number(window.localStorage.getItem("ksoundSoundboardColumns") || "0");
        let currentColumns = storedColumns || serverColumns || 0;
        let soundboardRevision = "{config_revision}";
        let refreshInFlight = false;
        let favoriteItems = [];
        let favoriteSlots = [];
        let favoriteEffects = [];
        let favoriteBusy = false;

        const favoriteLayoutKeys = {{width:"ksoundFavoritesWidthV2", columns:"ksoundFavoritesColumnsV2", height:"ksoundFavoritesTileHeightV2"}};
        let favoriteRenderSignature = "";
        function favoriteNumber(value, fallback, low, high) {{
          const n = Number(value);
          return Number.isFinite(n) && value !== null && value !== "" ? Math.max(low, Math.min(high, Math.round(n))) : fallback;
        }}
        function applyFavoriteLayout() {{
          const available = Math.max(90, window.innerWidth - 180);
          const wanted = favoriteNumber(localStorage.getItem(favoriteLayoutKeys.width), window.innerWidth < 540 ? 140 : 168, 120, 340);
          const width = Math.min(wanted, available);
          const columns = favoriteNumber(localStorage.getItem(favoriteLayoutKeys.columns), 1, 1, 4);
          const height = favoriteNumber(localStorage.getItem(favoriteLayoutKeys.height), 82, 52, 140);
          const root = document.documentElement.style;
          root.setProperty("--favorites-width", width + "px");
          root.setProperty("--favorites-columns", String(columns));
          root.setProperty("--favorites-tile-height", height + "px");
          for (const [id, value] of [["favoritesWidthOption", wanted], ["favoritesColumnsOption", columns], ["favoritesHeightOption", height]]) {{
            const node = document.getElementById(id); if (node) node.value = String(value);
          }}
          document.getElementById("favoritesWidthValue").textContent = width + " px";
          document.getElementById("favoritesHeightValue").textContent = height + " px";
        }}
        function setFavoriteLayout(kind, value) {{
          const key = favoriteLayoutKeys[kind];
          if (!key) return;
          const ranges = {{width:[120,340], columns:[1,4], height:[52,140]}};
          const [low, high] = ranges[kind];
          localStorage.setItem(key, String(favoriteNumber(value, low, low, high)));
          applyFavoriteLayout();
        }}
        function closeFavoriteEditor() {{ document.getElementById("favoriteEditor").hidden = true; }}
        function openFavoriteEditor() {{
          document.getElementById("favoriteEditor").hidden = false;
          renderFavoriteEditor();
        }}
        function favoriteKey(item) {{ return item.type + ":" + item.id; }}
        function reconcileFavorites(state) {{
          if (!state) return;
          favoriteItems = Array.isArray(state.favorites) ? state.favorites : [];
          favoriteSlots = Array.isArray(state.slots) ? state.slots : [];
          favoriteEffects = state.voice_fx && Array.isArray(state.voice_fx.effects) ? state.voice_fx.effects : [];
          renderFavorites();
          if (!document.getElementById("favoriteEditor").hidden) renderFavoriteEditor();
        }}
        function renderFavoritePins() {{
          const pinned = new Set(favoriteItems.map(favoriteKey));
          document.querySelectorAll(".favorite-pin[data-type][data-id]").forEach(pin => {{
            const selected = pinned.has(pin.dataset.type + ":" + pin.dataset.id);
            pin.textContent = selected ? "★" : "☆";
            pin.setAttribute("aria-pressed", selected ? "true" : "false");
            const label = pin.dataset.id || "item";
            pin.setAttribute("aria-label", (selected ? "Unpin " : "Pin ") + label);
            pin.title = selected ? "Remove favorite" : "Add favorite";
            pin.disabled = false;
          }});
          document.getElementById("favoritesCount").textContent = String(favoriteItems.length);
        }}
        function renderFavorites() {{
          const list = document.getElementById("favoritesActions");
          renderFavoritePins();
          const details = favoriteItems.filter(item => remoteModuleAvailability[item.type === "pad" ? "pads" : "voice"])
            .map(item => ({{item, meta:item.type === "pad"
              ? favoriteSlots.find(slot => slot.id === item.id)
              : favoriteEffects.find(fx => fx.id === item.id)}}));
          const signature = JSON.stringify(details);
          if (signature === favoriteRenderSignature) return;
          favoriteRenderSignature = signature;
          const scroll = list.scrollTop;
          list.replaceChildren();
          details.forEach(({{item,meta}}) => {{
            const button = document.createElement("button");
            button.type = "button";
            button.className = "favorite-tile";
            const label = meta ? String(meta.label || item.id) : item.id;
            button.title = (item.type === "pad" ? "Play " : "Toggle ") + label;
            button.disabled = !meta || (item.type === "pad" && !meta.has_sound);
            if (meta && item.type === "voice_fx") button.classList.toggle("active", !!meta.active);
            if (item.type === "pad" && meta) {{
              const colors = Array.isArray(meta.auto_colors) ? meta.auto_colors : [];
              button.style.setProperty("--favorite-color-a", String(colors[0] || "#18487e"));
              button.style.setProperty("--favorite-color-b", String(colors[1] || "#5a2d8e"));
              const url = backgroundUrlForSlot(meta);
              if (url) button.style.setProperty("--favorite-background", 'url("' + url.replace(/"/g,"%22") + '")');
            }}
            const kind = document.createElement("span"); kind.className = "favorite-kind";
            kind.textContent = item.type === "pad" ? "PAD" : "FX";
            const name = document.createElement("span"); name.className = "favorite-label"; name.textContent = label;
            button.append(kind, name);
            button.addEventListener("click", () => activateFavorite(item));
            list.appendChild(button);
          }});
          if (!list.children.length) {{
            const empty = document.createElement("small"); empty.className = "favorite-empty";
            empty.textContent = "Tap ☆ on a pad or Voice FX to pin it here.";
            list.appendChild(empty);
          }}
          list.scrollTop = scroll;
        }}
        function activateFavorite(item) {{
          if (item.type === "pad") {{
            if (editMode) {{ setRemoteStatus("Finish editing pads first"); return; }}
            const slot = favoriteSlots.find(value => value.id === item.id);
            if (slot && slot.has_sound) postAction("/play", "slot=" + encodeURIComponent(item.id), "Play " + slot.label + " sent");
          }} else if (item.type === "voice_fx") {{
            const button = Array.from(document.querySelectorAll(".voice-fx-card[data-effect]"))
              .find(value => value.dataset.effect === item.id);
            if (button) toggleVoiceFx(button);
          }}
        }}
        async function toggleFavorite(type, id) {{
          if (favoriteBusy) return;
          const key = type + ":" + id;
          const next = favoriteItems.filter(item => favoriteKey(item) !== key);
          if (next.length === favoriteItems.length) {{
            next.push({{type, id}});
          }}
          favoriteBusy = true;
          try {{
            const response = await fetch("/favorites?token={escaped_token}", {{
              method:"POST", headers:{{"Content-Type":"application/x-www-form-urlencoded", "X-Requested-With":"fetch"}},
              body:"items=" + encodeURIComponent(JSON.stringify(next))
            }});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || "Favorites unavailable");
            favoriteItems = data.favorites;
            renderFavorites(); renderFavoriteEditor();
            setRemoteStatus("Favorites saved");
          }} catch (_error) {{ setRemoteStatus("Could not save favorites"); }}
          finally {{ favoriteBusy = false; }}
        }}
        function renderFavoriteEditor() {{
          const container = document.getElementById("favoriteChoices");
          container.replaceChildren();
          const known = new Set(favoriteItems.map(favoriteKey));
          const sections = [
            ["Pads", "pad", remoteModuleAvailability.pads ? favoriteSlots : []],
            ["Voice FX", "voice_fx", remoteModuleAvailability.voice ? favoriteEffects : []]
          ];
          sections.forEach(([label, type, entries]) => {{
            if (!entries.length) return;
            const title = document.createElement("h3"); title.textContent = label; container.appendChild(title);
            entries.forEach(item => {{
              const button = document.createElement("button"); button.type = "button";
              button.className = "favorite-entry";
              const pinned = known.has(type + ":" + item.id);
              button.setAttribute("aria-pressed", pinned ? "true" : "false");
              const name = document.createElement("span"); name.textContent = item.label || item.id;
              const symbol = document.createElement("span"); symbol.textContent = pinned ? "★" : "☆";
              button.append(name, symbol);
              button.addEventListener("click", () => toggleFavorite(type, item.id));
              container.appendChild(button);
            }});
          }});
        }}
        let padImageObserver = null;
        let folderNodeIndex = new Map();
        let visiblePadWraps = [];
        let folderFilterInitialized = false;

        function setRemoteStatus(text) {{
          const node = document.getElementById("remoteStatus");
          if (node) node.textContent = text;
        }}

        function setVolumeState(text) {{
          const node = document.getElementById("volumeSendState");
          if (node) node.textContent = text;
        }}

        function setRemoteMode(mode, persist = true) {{
          let wanted = mode === "voice" ? "voice" : "pads";
          if (!remoteModuleAvailability[wanted]) wanted = defaultRemoteMode;
          remoteMode = wanted;
          if (persist) window.localStorage.setItem("ksoundRemoteModule", wanted);
          const pads = document.getElementById("padsModule");
          const voice = document.getElementById("voiceFxModule");
          if (pads) pads.hidden = wanted !== "pads";
          if (voice) voice.hidden = wanted !== "voice";
          document.body.classList.toggle("remote-mode-voice", wanted === "voice");
          const padsButton = document.getElementById("modulePadsButton");
          const voiceButton = document.getElementById("moduleVoiceButton");
          if (padsButton) padsButton.classList.toggle("active", wanted === "pads");
          if (voiceButton) voiceButton.classList.toggle("active", wanted === "voice");
          if (wanted === "voice" && editMode) setEditMode(false);
          const hint = document.getElementById("remoteGestureHint");
          if (hint) hint.textContent = wanted === "voice" ? "Tap an effect to toggle it · Active effects stay synchronized with K-Sounds" : (editMode ? EDIT_HINT : playHint());
          setRemoteStatus(wanted === "voice" ? "Voice FX ready" : "Ready");
        }}

        function reconcileVoiceFxState(state) {{
          if (!state || !Array.isArray(state.active)) return;
          const active = new Set(state.active.map(value => String(value || "").trim().toLowerCase()));
          const labels = [];
          document.querySelectorAll(".voice-fx-card[data-effect]").forEach(button => {{
            const effect = String(button.dataset.effect || "").trim().toLowerCase();
            const on = active.has(effect);
            button.classList.toggle("active", on);
            button.setAttribute("aria-pressed", on ? "true" : "false");
            const status = button.querySelector("small");
            if (status) status.textContent = on ? "Active" : "Tap to enable";
            if (on) {{ const title = button.querySelector("strong"); if (title) labels.push(title.textContent || effect); }}
          }});
          const summary = document.getElementById("voiceFxSummary");
          if (summary) summary.textContent = labels.length ? labels.join(" + ") : "Off";
          const strength = document.getElementById("voiceFxStrength");
          if (strength && Number.isFinite(Number(state.amount))) strength.textContent = "Strength " + Math.max(0, Math.min(100, Number(state.amount))) + "%";
          favoriteEffects = Array.isArray(state.effects) ? state.effects : favoriteEffects;
          renderFavorites();
        }}

        async function toggleVoiceFx(button) {{
          if (!button) return;
          const effect = String(button.dataset.effect || "").trim().toLowerCase();
          if (!effect || voiceFxPending.has(effect)) return;
          const enable = !button.classList.contains("active");
          voiceFxPending.add(effect); button.classList.add("sending");
          setRemoteStatus((enable ? "Enabling " : "Disabling ") + (button.querySelector("strong")?.textContent || effect) + "…");
          try {{
            const response = await fetch("/voice-fx?token={escaped_token}", {{ method:"POST", headers:{{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"fetch"}}, body:"effect=" + encodeURIComponent(effect) + "&enabled=" + (enable ? "true" : "false") }});
            const data = await response.json().catch(() => null);
            if (!response.ok || !data || data.ok !== true) throw new Error("Voice FX command failed");
            reconcileVoiceFxState(data.voice_fx); setRemoteStatus("Voice FX updated");
          }} catch (_error) {{ setRemoteStatus("Voice FX unavailable — K-Sounds Hub must be running"); refreshSettingsFromServer(); }}
          finally {{ voiceFxPending.delete(effect); button.classList.remove("sending"); }}
        }}

        function saveScrollPositionForReload() {{
          try {{
            const y = Math.max(
              0,
              Number(window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0)
            );
            window.sessionStorage.setItem("ksoundSoundboardScrollY", String(y));
          }} catch (_error) {{}}
        }}

        function restoreScrollPositionAfterReload() {{
          let y = null;

          try {{
            if ("scrollRestoration" in history) {{
              history.scrollRestoration = "manual";
            }}

            const raw = window.sessionStorage.getItem("ksoundSoundboardScrollY");
            if (raw !== null) {{
              window.sessionStorage.removeItem("ksoundSoundboardScrollY");
              y = Math.max(0, Number(raw) || 0);
            }}
          }} catch (_error) {{
            y = null;
          }}

          if (y === null) return;

          const restore = () => window.scrollTo(0, y);
          requestAnimationFrame(restore);
          window.setTimeout(restore, 80);
          window.setTimeout(restore, 260);
        }}

        function padWraps() {{
          return Array.from(document.querySelectorAll(".pad-wrap"));
        }}

        function backgroundUrlForSlot(slot) {{
          if (!slot || !slot.has_background || !slot.background_version) return "";
          return "/background?token={escaped_token}"
            + "&slot=" + encodeURIComponent(String(slot.id || ""))
            + "&rev=" + encodeURIComponent(String(slot.background_version || ""));
        }}

        function loadPadBackground(wrap) {{
          if (!wrap) return;
          const button = wrap.querySelector(".pad");
          if (!button) return;
          const url = String(wrap.dataset.backgroundUrl || "");
          if (!url) {{
            button.classList.remove("has-bg");
            button.style.removeProperty("--pad-bg");
            return;
          }}
          button.style.setProperty("--pad-bg", 'url("' + url.replace(/"/g, "%22") + '")');
          button.classList.add("has-bg");
          wrap.dataset.backgroundLoaded = url;
        }}

        function ensurePadImageObserver() {{
          if (padImageObserver || !("IntersectionObserver" in window)) return;
          padImageObserver = new IntersectionObserver(entries => {{
            entries.forEach(entry => {{
              if (!entry.isIntersecting) return;
              loadPadBackground(entry.target);
              padImageObserver.unobserve(entry.target);
            }});
          }}, {{ rootMargin: "220px 0px" }});
        }}

        function observePadBackground(wrap) {{
          if (!wrap || !wrap.dataset.backgroundUrl) return;
          if (wrap.dataset.backgroundLoaded === wrap.dataset.backgroundUrl) return;
          ensurePadImageObserver();
          if (padImageObserver) padImageObserver.observe(wrap);
          else if (!wrap.hidden) loadPadBackground(wrap);
        }}

        function applySlotToPadWrap(wrap, slot, index) {{
          const slotId = String((slot && slot.id) || "");
          const label = String((slot && slot.label) || slotId || ("SOUND " + (index + 1)));
          const button = wrap.querySelector(".pad");
          const title = button ? button.querySelector("strong") : null;
          const colors = Array.isArray(slot && slot.auto_colors) ? slot.auto_colors : [];
          const colorA = String(colors[0] || "#18487e");
          const colorB = String(colors[1] || "#5a2d8e");
          const backgroundUrl = backgroundUrlForSlot(slot);

          wrap.dataset.slot = slotId;
          const pin = wrap.querySelector(".favorite-pin");
          if (pin) pin.dataset.id = slotId;
          wrap.dataset.folder = String((slot && slot.folder) || "");
          wrap.dataset.label = label;
          wrap.dataset.index = String(index);
          wrap.style.setProperty("--pad-auto-a", colorA);
          wrap.style.setProperty("--pad-auto-b", colorB);

          if (button) {{
            button.dataset.slot = slotId;
            button.dataset.label = label;
            button.disabled = !(slot && slot.has_sound);
            button.style.setProperty("--pad-auto-a", colorA);
            button.style.setProperty("--pad-auto-b", colorB);
          }}
          if (title) title.textContent = label;

          if (wrap.dataset.backgroundUrl !== backgroundUrl) {{
            wrap.dataset.backgroundUrl = backgroundUrl;
            wrap.dataset.backgroundLoaded = "";
            if (button) {{
              button.classList.remove("has-bg");
              button.style.removeProperty("--pad-bg");
            }}
          }}
          observePadBackground(wrap);
        }}

        function createPadWrap(slot, index) {{
          const wrap = document.createElement("div");
          wrap.className = "pad-wrap";
          const button = document.createElement("button");
          button.className = "pad";
          button.type = "button";
          const title = document.createElement("strong");
          button.appendChild(title);
          button.addEventListener("click", () => playSlotFromButton(button));
          wrap.appendChild(button);
          const pin = document.createElement("button");
          pin.type = "button";
          pin.className = "favorite-pin";
          pin.dataset.type = "pad";
          pin.dataset.id = String(slot.id || "");
          pin.addEventListener("click", () => toggleFavorite("pad", pin.dataset.id));
          wrap.appendChild(pin);
          applySlotToPadWrap(wrap, slot, index);
          return wrap;
        }}

        function rebuildFolderNodeIndex() {{
          const all = padWraps();
          const index = new Map();
          index.set("__all__", all);
          all.forEach(wrap => {{
            const key = normalizeFolderValue(wrap.dataset.folder || "");
            if (!index.has(key)) index.set(key, []);
            index.get(key).push(wrap);
          }});
          folderNodeIndex = index;
          folderFilterInitialized = false;
        }}

        function reconcilePads(slots) {{
          const grid = document.getElementById("padsGrid");
          if (!grid || !Array.isArray(slots)) return;
          grid.querySelectorAll(".empty").forEach(node => node.remove());

          const existing = new Map(padWraps().map(wrap => [String(wrap.dataset.slot || ""), wrap]));
          const retained = new Set();
          slots.forEach((slot, index) => {{
            const slotId = String((slot && slot.id) || "");
            let wrap = existing.get(slotId);
            if (!wrap) wrap = createPadWrap(slot, index);
            else applySlotToPadWrap(wrap, slot, index);
            retained.add(wrap);
            grid.appendChild(wrap);
            bindPadGesture(wrap);
          }});
          existing.forEach(wrap => {{
            if (!retained.has(wrap)) {{
              if (padImageObserver) padImageObserver.unobserve(wrap);
              wrap.remove();
            }}
          }});

          if (!slots.length) {{
            const empty = document.createElement("p");
            empty.className = "empty";
            empty.textContent = "No pads found. Configure the soundboard in K-Sounds Hub first.";
            grid.appendChild(empty);
          }}
          refreshPadIndexes();
          rebuildFolderNodeIndex();
          updateSoundSelection();
          renderFavoritePins();
        }}

        function bindFolderButton(button) {{
          if (!button || button.dataset.folderBound === "1") return;
          button.dataset.folderBound = "1";
          button.addEventListener("click", () => selectFolder(button.dataset.folder || ""));
        }}

        function reconcileFolders(folders) {{
          const bar = document.querySelector(".folder-bar");
          if (!bar || !Array.isArray(folders)) return;
          const definitions = [{{ value: "__all__", label: "All" }}];
          folders.forEach(raw => {{
            const label = String(raw || "").trim();
            if (!label) return;
            definitions.push({{
              value: label.toLowerCase() === "main" ? "" : label,
              label: label
            }});
          }});

          const existing = new Map(
            Array.from(bar.querySelectorAll(".folder-pill"))
              .map(button => [normalizeFolderValue(button.dataset.folder || ""), button])
          );
          const retained = new Set();
          const valid = new Set();
          definitions.forEach(item => {{
            const key = normalizeFolderValue(item.value);
            let button = existing.get(key);
            if (!button) {{
              button = document.createElement("button");
              button.type = "button";
              button.className = "folder-pill";
            }}
            button.dataset.folder = item.value;
            button.textContent = item.label;
            bindFolderButton(button);
            retained.add(button);
            valid.add(key);
            bar.appendChild(button);
          }});
          existing.forEach(button => {{ if (!retained.has(button)) button.remove(); }});

          if (!valid.has(normalizeFolderValue(activeFolder))) {{
            activeFolder = "__all__";
            window.localStorage.setItem("ksoundSoundboardFolderV2", activeFolder);
          }}
        }}

        function reconcileSoundboardState(data) {{
          if (!data) return;
          reconcileFolders(Array.isArray(data.folders) ? data.folders : []);
          reconcilePads(Array.isArray(data.slots) ? data.slots : []);
          applyFolderFilter();
        }}

        function refreshPadIndexes() {{
          padWraps().forEach((node, index) => {{
            node.dataset.index = String(index);
          }});
        }}

        function orderPayload() {{
          return JSON.stringify(
            padWraps()
              .map(node => node.dataset.slot || "")
              .filter(Boolean)
          );
        }}

        function clearDropTargets() {{
          padWraps().forEach(node => node.classList.remove("drop-target"));
        }}

        function clearDragStateClasses() {{
          padWraps().forEach(node => node.classList.remove("drag-source", "drop-target", "swap-target"));
        }}

        function labelOfWrap(wrap) {{
          if (!wrap) return "";
          return wrap.dataset.label || wrap.dataset.slot || "";
        }}

        function clampColumns(value) {{
          const n = Math.round(Number(value) || 4);
          return Math.max(GRID_MIN_COLUMNS, Math.min(GRID_MAX_COLUMNS, n));
        }}

        function clampColumnsFloat(value) {{
          const n = Number(value) || 4;
          return Math.max(GRID_MIN_COLUMNS, Math.min(GRID_MAX_COLUMNS, n));
        }}

        function estimateColumns() {{
          const wraps = padWraps();
          if (!wraps.length) return clampColumns(currentColumns || 4);

          const firstTop = wraps[0].offsetTop;
          let count = 0;
          for (const wrap of wraps) {{
            if (Math.abs(wrap.offsetTop - firstTop) <= 4) count += 1;
            else break;
          }}
          return clampColumns(count || currentColumns || 4);
        }}

        function visualScaleForColumns(columns) {{
          const safeColumns = clampColumnsFloat(columns);

          const points = [
            [1, 2.00],
            [2, 1.62],
            [3, 1.28],
            [4, 1.00],
            [5, 0.88],
            [6, 0.78],
            [7, 0.70],
            [8, 0.64],
            [9, 0.59],
            [10, 0.55],
            [11, 0.51],
            [12, 0.48],
            [13, 0.45]
          ];

          if (safeColumns <= points[0][0]) return points[0][1];
          if (safeColumns >= points[points.length - 1][0]) return points[points.length - 1][1];

          for (let i = 0; i < points.length - 1; i++) {{
            const left = points[i];
            const right = points[i + 1];

            if (safeColumns >= left[0] && safeColumns <= right[0]) {{
              const t = (safeColumns - left[0]) / (right[0] - left[0]);
              return left[1] + ((right[1] - left[1]) * t);
            }}
          }}

          return 1.0;
        }}

        function applyPadVisualScale(columns) {{
          const factor = visualScaleForColumns(columns);

          const rootStyle = document.documentElement.style;
          rootStyle.setProperty("--ksh-pad-gap", Math.round(9 * factor) + "px");
          rootStyle.setProperty("--ksh-pad-height", Math.round(108 * factor) + "px");
          rootStyle.setProperty("--ksh-pad-padding", Math.round(14 * factor) + "px");
          rootStyle.setProperty("--ksh-pad-radius", Math.round(20 * factor) + "px");
          rootStyle.setProperty("--ksh-wrap-radius", Math.round(22 * factor) + "px");
          rootStyle.setProperty("--ksh-title-font", Math.max(12, Math.round(17 * factor)) + "px");
          rootStyle.setProperty("--ksh-subtitle-font", Math.max(10, Math.round(12 * factor)) + "px");
          rootStyle.setProperty("--ksh-title-margin", Math.max(4, Math.round(8 * factor)) + "px");
        }}

        function setGridColumns(columns) {{
          currentColumns = clampColumns(columns);
          const grid = document.getElementById("padsGrid");
          if (grid) {{
            grid.style.gridTemplateColumns = "repeat(" + currentColumns + ", minmax(0, 1fr))";
          }}
        }}

        function saveColumnsToServer(columns) {{
          const safeColumns = clampColumns(columns);

          if (layoutSaveTimer) {{
            clearTimeout(layoutSaveTimer);
          }}

          layoutSaveTimer = window.setTimeout(() => {{
            fetch("/layout?token={escaped_token}", {{
              method: "POST",
              headers: {{
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "fetch"
              }},
              body: "columns=" + encodeURIComponent(String(safeColumns))
            }}).catch(() => {{}});
          }}, 120);
        }}

        function applyColumns(columns, persist = true, visualColumns = null) {{
          setGridColumns(columns);
          applyPadVisualScale(visualColumns === null ? currentColumns : visualColumns);

          const colsOption = document.getElementById("padsColumnsOption");
          if (colsOption) colsOption.value = String(currentColumns);
          const colsValue = document.getElementById("padsColumnsValue");
          if (colsValue) colsValue.textContent = String(currentColumns);
          if (persist) {{
            window.localStorage.setItem("ksoundSoundboardColumns", String(currentColumns));
            saveColumnsToServer(currentColumns);
          }}
        }}

        function pointerDistance(a, b) {{
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          return Math.hypot(dx, dy);
        }}

        function updatePointer(event) {{
          if (activePointers.has(event.pointerId)) {{
            activePointers.set(event.pointerId, {{ x: event.clientX, y: event.clientY }});
          }}
        }}

        function releaseDragCapture() {{
          if (!dragState || !dragState.wrap) return;

          try {{
            dragState.wrap.releasePointerCapture?.(dragState.pointerId);
          }} catch (_error) {{}}
        }}

        function finishPinch(commit = true) {{
          if (!pinchState) {{
            activePointers.clear();
            return false;
          }}

          const commitColumns = clampColumns(currentColumns || pinchState.startColumns || estimateColumns());

          pinchState = null;
          activePointers.clear();
          releaseDragCapture();
          dragState = null;
          clearDragStateClasses();

          if (commit) {{
            window.localStorage.setItem("ksoundSoundboardColumns", String(commitColumns));
            saveColumnsToServer(commitColumns);
            setRemoteStatus("Grid: " + commitColumns + " per row");
          }}

          gestureCooldownUntil = Date.now() + 220;
          return true;
        }}

        function hardResetGesture(message = "") {{
          cancelLongPress(); stopDragPreview();
          if (pinchState) {{
            finishPinch(true);
          }} else {{
            activePointers.clear();
            releaseDragCapture();
            dragState = null;
            clearDragStateClasses();
            cancelLongPress();
          }}

          if (message) setRemoteStatus(message);
        }}

        function startPinchIfReady() {{
          cancelLongPress(); stopDragPreview();
          if (!pinchAllowed() || activePointers.size < 2) return false;

          const points = Array.from(activePointers.values()).slice(0, 2);
          const distance = pointerDistance(points[0], points[1]);
          if (distance <= 0) return false;

          const startColumns = currentColumns ? clampColumns(currentColumns) : estimateColumns();

          releaseDragCapture();

          pinchState = {{
            startDistance: distance,
            startColumns: startColumns
          }};

          dragState = null;
          clearDragStateClasses();
          setRemoteStatus((editMode ? "Edit mode" : "Play mode") + ": pinch to resize grid · " + startColumns + " per row");
          return true;
        }}

        function updatePinchColumns() {{
          if (!pinchState || activePointers.size < 2) return false;

          const points = Array.from(activePointers.values()).slice(0, 2);
          const distance = pointerDistance(points[0], points[1]);
          if (distance <= 0 || pinchState.startDistance <= 0) return true;

          // Discrete proportional pinch: each ~8% relative distance change is one grid level.
          // Unlike a fixed pixel threshold, this lets one normal pinch traverse the full 1..13
          // range even on smaller tablets, while keeping every visible change on an integer step.
          const ratio = pinchState.startDistance / distance;
          const stepDelta = Math.round(
            Math.log(Math.max(0.0001, ratio)) / Math.log(PINCH_RATIO_PER_STEP)
          );
          const nextColumns = clampColumns(pinchState.startColumns + stepDelta);

          if (nextColumns !== currentColumns) {{
            applyColumns(nextColumns, false);
            setRemoteStatus("Grid: " + nextColumns + " per row");
          }}

          return true;
        }}

        async function saveCurrentOrder() {{
          return postAction(
            "/reorder",
            "order=" + encodeURIComponent(orderPayload()),
            "Order saved"
          );
        }}

        function pinchAllowed() {{
          return editMode || allowPinchOutsideEdit;
        }}

        function applyGestureModeVisuals() {{
          document.body.classList.toggle("pinch-play-mode", !editMode && allowPinchOutsideEdit);
        }}

        function applyEditModeVisuals() {{
          document.body.classList.toggle("edit-mode", editMode);
          applyGestureModeVisuals();
          const hint = document.getElementById("remoteGestureHint");
          if (hint) hint.textContent = editMode ? EDIT_HINT : playHint();

          const editButton = document.getElementById("editButton");
          if (editButton) {{
            editButton.textContent = editMode ? "✓" : "✎";
            editButton.setAttribute("aria-pressed", editMode ? "true" : "false");
            editButton.setAttribute("aria-label", editMode ? "Done editing" : "Edit folders and order");
            editButton.title = editMode ? "Done editing" : "Edit folders and order";
          }}
        }}

        function setEditMode(enabled, announce = true) {{
          editMode = Boolean(enabled);

          try {{
            window.sessionStorage.setItem("ksoundSoundboardEditMode", editMode ? "1" : "0");
          }} catch (_error) {{}}

          hardResetGesture();
          applyEditModeVisuals();
          if (!editMode) {{ selectedSoundIds.clear(); closeMoveFolderDialog(); }}
          updateSoundSelection();

          if (announce) {{
            setRemoteStatus(editMode ? EDIT_HINT : playHint());
          }}

          if (!editMode) {{
            window.setTimeout(refreshSettingsFromServer, 80);
          }}
        }}

        function toggleEditMode() {{
          const now = Date.now();

          // Some Android WebViews can emit duplicate click/tap events.
          // Edit must behave as a manual toggle, not flicker ON then OFF.
          if (now - lastEditToggleAt < 350) {{
            return;
          }}

          lastEditToggleAt = now;
          setEditMode(!editMode, true);
        }}

        async function postAction(path, body, okText) {{
          try {{
            const response = await fetch(path + "?token={escaped_token}", {{
              method: "POST",
              headers: {{
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "fetch"
              }},
              body: body || ""
            }});
            if (!response.ok) throw new Error("HTTP " + response.status);
            setRemoteStatus(okText);
            return true;
          }} catch (error) {{
            setRemoteStatus("Send error");
            return false;
          }}
        }}

        function playSlotFromButton(button) {{
          if (editMode) {{
            return;
          }}

          if (pinchState || activePointers.size > 0 || Date.now() < gestureCooldownUntil) {{
            setRemoteStatus("Gesture active");
            return;
          }}

          const slot = button.dataset.slot || "";
          if (!slot || button.disabled) return;
          button.classList.add("sending");
          window.setTimeout(() => button.classList.remove("sending"), 140);
          postAction("/play", "slot=" + encodeURIComponent(slot), "Play " + slot + " sent");
        }}

        function dragTargetFromPoint(clientX, clientY) {{
          const element = document.elementFromPoint(clientX, clientY);
          if (!element) return null;
          return element.closest(".pad-wrap");
        }}

        function pointerIsInsidePadsGrid(event) {{
          const target = event.target;
          return Boolean(target && target.closest && target.closest("#padsGrid"));
        }}

        function cancelPointerEvent(event) {{
          event.preventDefault();
          event.stopPropagation();
          event.stopImmediatePropagation?.();
        }}

        function forcePinchPriority(event) {{
          activePointers.set(event.pointerId, {{ x: event.clientX, y: event.clientY }});

          if (activePointers.size < 2) {{
            return false;
          }}

          releaseDragCapture();
          dragState = null;
          clearDragStateClasses();

          if (!pinchState) {{
            startPinchIfReady();
          }}

          updatePinchColumns();
          cancelPointerEvent(event);
          return true;
        }}

        function onGlobalPointerDown(event) {{
          if (event.target.closest && event.target.closest(".favorite-pin")) return;
          if (!pinchAllowed()) return;
          if (event.button !== undefined && event.button !== 0) return;

          // First finger must start on the pads grid.
          // Once one finger is already active, any second finger immediately becomes pinch.
          if (!pointerIsInsidePadsGrid(event) && activePointers.size === 0) {{
            return;
          }}

          if (Date.now() < gestureCooldownUntil) {{
            cancelPointerEvent(event);
            return;
          }}

          forcePinchPriority(event);
        }}

        function syncActivePointersFromTouches(touches) {{
          activePointers.clear();

          for (let i = 0; i < touches.length; i++) {{
            const touch = touches[i];
            activePointers.set("touch-" + i, {{ x: touch.clientX, y: touch.clientY }});
          }}
        }}

        function forceTouchPinchPriority(event) {{
          if (!pinchAllowed()) return false;
          if (!event.touches || event.touches.length < 2) return false;

          syncActivePointersFromTouches(event.touches);

          releaseDragCapture();
          dragState = null;
          clearDragStateClasses();

          if (!pinchState) {{
            startPinchIfReady();
          }}

          updatePinchColumns();
          cancelPointerEvent(event);
          return true;
        }}

        function onGlobalTouchStart(event) {{
          if (!pinchAllowed()) return;

          // Android/WebView fallback:
          // as soon as two fingers exist, pinch wins over pad press/drag/click.
          forceTouchPinchPriority(event);
        }}

        function onGlobalTouchMove(event) {{
          if (pinchAllowed() && event.touches && event.touches.length >= 2) {{
            forceTouchPinchPriority(event);
            return;
          }}

          // Fallback for Android/WebView: if a promoted touch drag loses its PointerEvent
          // stream, keep driving the preview from TouchEvents until release.
          if (dragState && dragState.active && event.touches && event.touches.length === 1) {{
            const touch = event.touches[0];
            dragState.x = touch.clientX;
            dragState.y = touch.clientY;
            cancelPointerEvent(event);
            return;
          }}

        }}

        function onGlobalTouchEnd(event) {{
          if (pinchAllowed() && event.touches && event.touches.length >= 2) {{
            forceTouchPinchPriority(event);
            return;
          }}

          if (pinchState) {{
            finishPinch(true);
            cancelPointerEvent(event);
            return;
          }}

          if (dragState && dragState.active && (!event.touches || event.touches.length === 0)) {{
            if (event.type === "touchcancel") {{
              dragState = null;
              stopDragPreview();
              clearDragStateClasses();
              activePointers.clear();
              gestureCooldownUntil = Date.now() + 250;
              return;
            }}
            const touch = event.changedTouches && event.changedTouches.length ? event.changedTouches[0] : null;
            const state = dragState;
            // Reuse the normal release path. It clears dragState synchronously before saving
            // the order, so a later compatibility pointerup cannot commit the move twice.
            onPadPointerUp({{
              pointerId: state.pointerId,
              clientX: touch ? touch.clientX : state.x,
              clientY: touch ? touch.clientY : state.y,
              preventDefault: () => {{}}
            }});
            cancelPointerEvent(event);
            return;
          }}

          if (!event.touches || event.touches.length === 0) {{
            activePointers.clear();
          }}
        }}

        function onGlobalClick(event) {{
          if (event.target.closest && event.target.closest(".favorite-pin")) return;
          const wrap = event.target.closest && event.target.closest(".pad-wrap");
          if (!wrap) return;
          if (pinchState || activePointers.size >= 2 || Date.now() < gestureCooldownUntil || orderSavePending) {{
            cancelPointerEvent(event); return;
          }}
          if (!editMode) return;
          cancelPointerEvent(event);
          toggleSoundSelection(wrap);
        }}

        function updateSoundSelection() {{
          const wraps = padWraps();
          const ids = new Set(wraps.map(w => w.dataset.slot));
          selectedSoundIds.forEach(id => {{ if (!ids.has(id)) selectedSoundIds.delete(id); }});
          wraps.forEach(wrap => {{
            const selected = editMode && selectedSoundIds.has(wrap.dataset.slot);
            wrap.classList.toggle("selected", selected);
            const button = wrap.querySelector(".pad");
            if (button) {{
              if (editMode) button.setAttribute("aria-pressed", selected ? "true" : "false");
              else button.removeAttribute("aria-pressed");
            }}
          }});
          document.getElementById("selectionBar").hidden = !editMode;
          document.getElementById("selectionCount").textContent = selectedSoundIds.size + " selected";
          document.getElementById("swapSelectedButton").disabled = selectedSoundIds.size !== 2 || selectionMovePending || orderSavePending;
          document.getElementById("moveSelectedButton").disabled = !selectedSoundIds.size || selectionMovePending;
          const remove = document.getElementById("removeFolderButton");
          remove.hidden = !editMode || !activeFolder || activeFolder === "__all__";
          remove.disabled = selectionMovePending || !visiblePadWraps.some(w => selectedSoundIds.has(w.dataset.slot));
        }}
        function clearSoundSelection() {{ selectedSoundIds.clear(); updateSoundSelection(); }}
        function selectVisibleSounds() {{
          visiblePadWraps.forEach(wrap => selectedSoundIds.add(wrap.dataset.slot));
          updateSoundSelection();
        }}
        function toggleSoundSelection(wrap) {{
          if (selectionMovePending || orderSavePending) return;
          const id = wrap.dataset.slot;
          if (selectedSoundIds.has(id)) selectedSoundIds.delete(id); else selectedSoundIds.add(id);
          updateSoundSelection();
        }}
        async function swapSelectedSounds() {{
          if (!editMode || selectedSoundIds.size !== 2 || selectionMovePending || orderSavePending) return;
          const ids = [...selectedSoundIds];
          const first = padWraps().find(w => w.dataset.slot === ids[0]);
          const second = padWraps().find(w => w.dataset.slot === ids[1]);
          if (!first || !second || first === second || first.parentNode !== second.parentNode) return;
          hardResetGesture();
          const parent = first.parentNode;
          const original = padWraps();
          const marker = document.createComment("swap-selected");
          parent.insertBefore(marker, first);
          parent.insertBefore(first, second);
          parent.insertBefore(second, marker);
          marker.remove();
          refreshPadIndexes(); rebuildFolderNodeIndex(); applyFolderFilter();
          orderSavePending = true; updateSoundSelection();
          try {{
            if (await saveCurrentOrder()) {{
              selectedSoundIds.clear();
              setRemoteStatus("Selected pads swapped");
            }} else {{
              original.forEach(node => parent.appendChild(node));
              refreshPadIndexes(); rebuildFolderNodeIndex(); applyFolderFilter();
              setRemoteStatus("Swap not saved. Layout restored; reconnect to check.");
            }}
          }} finally {{
            orderSavePending = false;
            updateSoundSelection();
          }}
        }}
        function openMoveFolderDialog() {{
          if (!selectedSoundIds.size || selectionMovePending) return;
          hardResetGesture();
          const select = document.getElementById("moveFolderSelect");
          select.disabled = false;
          document.getElementById("moveTitle").textContent = "Move selected sounds";
          document.getElementById("confirmFolderMove").textContent = "Move sounds";
          delete document.getElementById("folderMoveDialog").dataset.removeIds;
          select.replaceChildren();
          document.querySelectorAll(".folder-pill").forEach(button => {{
            if (button.dataset.folder === "__all__") return;
            select.add(new Option(button.textContent, button.dataset.folder || ""));
          }});
          const newOption = new Option("New folder…", "__new__");
          newOption.dataset.newFolder = "1";
          select.add(newOption);
          document.getElementById("newFolderName").value = "";
          document.getElementById("moveError").textContent = "";
          updateMoveDestination();
          document.getElementById("folderMoveDialog").showModal();
        }}
        function closeMoveFolderDialog() {{
          if (!selectionMovePending) document.getElementById("folderMoveDialog").close();
        }}
        function updateMoveDestination() {{
          const choice = document.getElementById("moveFolderSelect");
          const isNew = choice.selectedOptions[0]?.dataset.newFolder === "1";
          document.getElementById("newFolderLabel").hidden = !isNew;
          if (isNew) document.getElementById("newFolderName").focus();
        }}
        function removeSelectedFromFolder() {{
          if (!editMode || !activeFolder || activeFolder === "__all__" || selectionMovePending) return;
          const ids = visiblePadWraps.filter(w => selectedSoundIds.has(w.dataset.slot)).map(w => w.dataset.slot);
          if (!ids.length) return;
          openMoveFolderDialog();
          document.getElementById("moveTitle").textContent = "Remove from " + activeFolder;
          document.getElementById("moveError").textContent = "Sounds remain in All and Main. Audio files are kept.";
          document.getElementById("confirmFolderMove").textContent = "Remove from folder";
          document.getElementById("moveFolderSelect").value = "";
          document.getElementById("moveFolderSelect").disabled = true;
          document.getElementById("folderMoveDialog").dataset.removeIds = JSON.stringify(ids);
          updateMoveDestination();
        }}
        async function moveSelectedToFolder() {{
          if (selectionMovePending || !selectedSoundIds.size) return;
          const choice = document.getElementById("moveFolderSelect");
          const value = choice.value;
          const isNew = choice.selectedOptions[0]?.dataset.newFolder === "1";
          const folder = isNew ? document.getElementById("newFolderName").value.trim() : value;
          const removing = document.getElementById("folderMoveDialog").dataset.removeIds;
          const moveIds = removing ? JSON.parse(removing) : [...selectedSoundIds];
          const previousFolder = activeFolder;
          const errorNode = document.getElementById("moveError");
          if (isNew && !folder) {{ errorNode.textContent = "Enter a folder name."; return; }}
          selectionMovePending = true;
          const confirm = document.getElementById("confirmFolderMove");
          confirm.disabled = true;
          errorNode.textContent = "Moving sounds…";
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 6000);
          try {{
            const response = await fetch("/move-to-folder?token={escaped_token}", {{
              method: "POST", headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
              body: new URLSearchParams({{ ids: JSON.stringify(moveIds), folder, ...(removing ? {{remove_from: previousFolder}} : {{}}) }}),
              signal: controller.signal
            }});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || "Move not confirmed. Selection kept.");
            moveIds.forEach(id => selectedSoundIds.delete(id));
            reconcileSoundboardState(data.state);
            soundboardRevision = data.state.revision;
            selectFolder(removing ? previousFolder : data.folder);
            document.getElementById("folderMoveDialog").close();
            setRemoteStatus(removing ? "Removed from folder · Available in All" : "Moved to " + (data.folder || "Main"));
          }} catch (error) {{
            errorNode.textContent = error.name === "AbortError" ? "Connection timed out. Reload to check; selection kept." : error.message;
          }} finally {{
            clearTimeout(timeout);
            selectionMovePending = false;
            confirm.disabled = false;
            updateSoundSelection();
          }}
        }}
        function cancelLongPress() {{ clearTimeout(longPressTimer); longPressTimer = 0; }}
        function stopDragPreview() {{
          cancelAnimationFrame(dragFrame); dragFrame = 0;
          document.querySelectorAll(".drag-ghost, .drop-marker").forEach(node => node.remove());
        }}
        function dragPlacement(state) {{
          const hit=document.elementFromPoint(state.x,state.y);
          if (hit && hit.closest(".folder-bar, .bottom-bar, .selection-bar, dialog, #remoteOptionsPanel")) return null;
          const x = state.x + scrollX, y = state.y + scrollY;
          let best = null, score = Infinity;
          for (const item of state.layout) {{
            const r = item.rect;
            const dx = Math.max(r.left - x, 0, x - r.right);
            const dy = Math.max(r.top - y, 0, y - r.bottom);
            const distance = dx * dx + dy * dy;
            if (distance < score) {{ score = distance; best = item; }}
          }}
          // Outside the grid: release cancels. Gaps between adjacent pads remain usable.
          if (!best || score > 36 * 36 || best.wrap === state.wrap) return null;
          const r = best.rect;
          return {{ wrap: best.wrap, rect: r, after: state.singleColumn ? y > (r.top+r.bottom)/2 : x > (r.left+r.right)/2 }};
        }}
        function paintDrag(now) {{
          const state = dragState;
          if (!state || !state.active) {{ stopDragPreview(); return; }}
          const elapsed = Math.min(32, now - (state.lastFrame || now)); state.lastFrame = now;
          const edge = 65, bottom = innerHeight - (editMode ? 200 : 100);
          const speed = state.y < edge ? -(edge - state.y) / edge :
            state.y > bottom ? Math.min(1, (state.y - bottom) / edge) : 0;
          if (speed) window.scrollBy(0, speed * elapsed * .8);
          state.ghost.style.transform = "translate(" + (state.x - state.ghostWidth/2) + "px," + (state.y - state.ghostHeight*.72) + "px)";
          const placement = dragPlacement(state);
          if (state.target && (!placement || state.target !== placement.wrap)) {{
            state.target.classList.remove("swap-target");
          }}
          state.target = placement ? placement.wrap : null;
          state.after = placement ? placement.after : false;
          if (state.inserting) {{
            state.marker.hidden = !placement;
            if (placement) {{
              const r = placement.rect;
              Object.assign(state.marker.style, state.singleColumn ? {{
                left: (r.left-scrollX)+"px", top: ((placement.after?r.bottom:r.top)-scrollY-3)+"px", width:(r.right-r.left)+"px", height:"6px"
              }} : {{
                left: ((placement.after?r.right:r.left)-scrollX-3)+"px", top:(r.top-scrollY)+"px", width:"6px", height:(r.bottom-r.top)+"px"
              }});
            }}
          }} else {{
            state.marker.hidden = true;
            if (state.target) state.target.classList.add("swap-target");
          }}
          dragFrame = requestAnimationFrame(paintDrag);
        }}

        function onPadPointerDown(event) {{
          if (event.target.closest && event.target.closest(".favorite-pin")) return;
          if (selectionMovePending || orderSavePending || (event.button !== undefined && event.button !== 0)) return;
          if (Date.now() < gestureCooldownUntil) {{ cancelPointerEvent(event); return; }}

          activePointers.set(event.pointerId, {{ x:event.clientX, y:event.clientY }});

          // Two fingers belong exclusively to pinch when the current mode allows it.
          if (activePointers.size >= 2) {{
            if (pinchAllowed()) {{
              forcePinchPriority(event);
            }} else {{
              hardResetGesture();
              gestureCooldownUntil = Date.now() + 300;
            }}
            return;
          }}

          const wrap = event.currentTarget;
          dragState = {{
            wrap,
            pointerId:event.pointerId,
            startX:event.clientX,
            startY:event.clientY,
            lastY:event.clientY,
            x:event.clientX,
            y:event.clientY,
            active:false,
            scrolled:false,
            target:null,
            // Edit reorders by insertion; normal/play mode swaps two pads.
            inserting:editMode
          }};

          cancelLongPress();
          const state = dragState;
          longPressTimer = setTimeout(() => {{
            if (dragState !== state || state.scrolled || pinchState || activePointers.size !== 1) return;
            state.active = true;
            const rect = wrap.getBoundingClientRect();
            state.layout = visiblePadWraps.map(w => {{
              const r = w.getBoundingClientRect();
              return {{wrap:w,rect:{{left:r.left+scrollX,right:r.right+scrollX,top:r.top+scrollY,bottom:r.bottom+scrollY}}}};
            }});
            state.singleColumn = state.layout.length < 2 || state.layout.every(item => Math.abs(item.rect.left-state.layout[0].rect.left) < 3);
            state.ghostWidth = Math.min(130, rect.width*.8);
            state.ghostHeight = state.ghostWidth*.84;
            state.ghost = wrap.cloneNode(true);
            state.ghost.className = "drag-ghost";
            state.ghost.style.width = state.ghostWidth + "px";
            state.ghost.setAttribute("aria-hidden", "true");
            document.body.appendChild(state.ghost);
            state.marker = document.createElement("div");
            state.marker.className = "drop-marker";
            state.marker.hidden = true;
            document.body.appendChild(state.marker);
            wrap.classList.add("drag-source");
            try {{ wrap.setPointerCapture(event.pointerId); }} catch (_) {{}}
            if (navigator.vibrate) navigator.vibrate(15);
            setRemoteStatus(state.inserting ? "Release at the cyan line to insert" : "Drop on another pad to swap positions");
            dragFrame = requestAnimationFrame(paintDrag);
          }}, LONG_PRESS_MS);
        }}

        function onPadPointerMove(event) {{
          updatePointer(event);

          if (pinchAllowed() && (pinchState || activePointers.size >= 2)) {{
            cancelLongPress();
            stopDragPreview();
            if (!pinchState) startPinchIfReady();
            updatePinchColumns();
            cancelPointerEvent(event);
            return;
          }}

          if (!dragState || dragState.pointerId !== event.pointerId) return;
          const state = dragState;
          state.x = event.clientX;
          state.y = event.clientY;

          if (!state.active) {{
            const travelled = Math.hypot(event.clientX-state.startX, event.clientY-state.startY);
            if (!state.scrolled && travelled >= SCROLL_INTENT_PX) {{
              // Movement before the hold deadline means scroll, never drag. This is manual
              // because touch-action:none prevents Android/WebView from cancelling the held pad.
              state.scrolled = true;
              cancelLongPress();
            }}
            if (state.scrolled) {{
              const dy = state.lastY - event.clientY;
              state.lastY = event.clientY;
              if (dy) window.scrollBy(0, dy);
              cancelPointerEvent(event);
            }}
            return;
          }}

          cancelPointerEvent(event);
        }}

        async function onPadPointerUp(event) {{
          activePointers.delete(event.pointerId);
          cancelLongPress();

          if (pinchState) {{
            finishPinch(true);
            cancelPointerEvent(event);
            return;
          }}

          if (!dragState || dragState.pointerId !== event.pointerId) return;
          const state = dragState;

          if (state.active) {{
            state.x = event.clientX;
            state.y = event.clientY;
            const placement = dragPlacement(state);
            state.target = placement ? placement.wrap : null;
            state.after = placement ? placement.after : false;
          }}

          dragState = null;
          try {{ state.wrap.releasePointerCapture(event.pointerId); }} catch (_) {{}}
          stopDragPreview();
          clearDragStateClasses();

          if (!state.active) {{
            if (state.scrolled) {{
              gestureCooldownUntil = Date.now() + 260;
              cancelPointerEvent(event);
            }}
            return;
          }}

          gestureCooldownUntil = Date.now() + 350;
          cancelPointerEvent(event);
          if (!state.target || state.target === state.wrap) {{
            setRemoteStatus(editMode ? EDIT_HINT : playHint());
            return;
          }}

          const parent = state.wrap.parentNode;
          const original = padWraps();
          if (state.inserting) {{
            parent.insertBefore(state.wrap, state.after ? state.target.nextSibling : state.target);
          }} else {{
            const marker = document.createComment("swap");
            parent.insertBefore(marker, state.wrap);
            parent.insertBefore(state.wrap, state.target);
            parent.insertBefore(state.target, marker);
            marker.remove();
          }}

          const changed = padWraps().some((node,i) => node !== original[i]);
          refreshPadIndexes();
          rebuildFolderNodeIndex();
          applyFolderFilter();
          if (!changed) return;

          orderSavePending = true;
          try {{
            if (!await saveCurrentOrder()) {{
              original.forEach(node => parent.appendChild(node));
              refreshPadIndexes();
              rebuildFolderNodeIndex();
              applyFolderFilter();
              setRemoteStatus("Order not saved. Layout restored; reconnect to check.");
            }}
          }} finally {{
            orderSavePending = false;
          }}
        }}

        function onPadPointerCancel(event) {{
          activePointers.delete(event.pointerId);
          cancelLongPress();
          if (pinchState) {{ finishPinch(true); return; }}
          if (dragState && dragState.pointerId === event.pointerId) {{
            // Android WebView can still send pointercancel while TouchEvents continue. Keep an
            // already-promoted touch drag alive; onGlobalTouchMove/End owns it from here.
            if (dragState.active && event.pointerType === "touch") {{
              dragState.pointerCancelled = true;
              return;
            }}
            dragState = null;
            stopDragPreview();
            clearDragStateClasses();
            gestureCooldownUntil = Date.now() + 250;
          }}
        }}
        function bindPadGesture(wrap) {{
          if (!wrap || wrap.dataset.gestureBound === "1") return;
          wrap.dataset.gestureBound = "1";
          wrap.addEventListener("pointerdown", onPadPointerDown, {{ passive: false }});
          wrap.addEventListener("contextmenu", event => event.preventDefault());
          wrap.addEventListener("dragstart", event => event.preventDefault());
          wrap.addEventListener("lostpointercapture", onPadPointerCancel, {{ passive: false }});
        }}

        function initDragReorder() {{
          window.addEventListener("pointerdown", onGlobalPointerDown, {{ passive: false, capture: true }});
          window.addEventListener("touchstart", onGlobalTouchStart, {{ passive: false, capture: true }});
          window.addEventListener("touchmove", onGlobalTouchMove, {{ passive: false, capture: true }});
          window.addEventListener("touchend", onGlobalTouchEnd, {{ passive: false, capture: true }});
          window.addEventListener("touchcancel", onGlobalTouchEnd, {{ passive: false, capture: true }});
          window.addEventListener("click", onGlobalClick, {{ passive: false, capture: true }});

          padWraps().forEach(bindPadGesture);

          window.addEventListener("pointermove", onPadPointerMove, {{ passive: false }});
          window.addEventListener("pointerup", onPadPointerUp, {{ passive: false }});
          window.addEventListener("pointercancel", onPadPointerCancel, {{ passive: false }});
          window.addEventListener("blur", () => hardResetGesture("Ready"));

          document.addEventListener("visibilitychange", () => {{
            if (document.hidden) {{
              hardResetGesture("Ready");
            }} else {{
              refreshSettingsFromServer();
            }}
          }});
        }}


        async function refreshSettingsFromServer() {{
          if (refreshInFlight) return;
          if (document.hidden) return;
          if (editMode) return;
          if (pinchState || activePointers.size > 0 || orderSavePending) return;

          refreshInFlight = true;

          try {{
            const response = await fetch("/state?token={escaped_token}", {{
              method: "GET",
              cache: "no-cache",
              headers: {{ "X-Requested-With": "fetch" }}
            }});
            if (!response.ok) return;

            const data = await response.json();
            if (data) reconcileFavorites(data);
            if (data && data.voice_fx) reconcileVoiceFxState(data.voice_fx);
            if (editMode || pinchState || activePointers.size > 0 || orderSavePending) return;
            const revision = String((data && data.revision) || "");

            if (revision && soundboardRevision && revision !== soundboardRevision) {{
              reconcileSoundboardState(data);
              setRemoteStatus("Soundboard updated");
            }}

            if (revision) {{
              soundboardRevision = revision;
            }}

            if (!volumeDragging && !volumeTimer && data && Number.isFinite(Number(data.global_volume))) {{
              const volume = String(Math.max(0, Math.min(100, Number(data.global_volume))));
              const slider = document.getElementById("globalVolumeSlider");
              const valueNode = document.getElementById("globalVolValue");

              if (slider && String(slider.value) !== volume) {{
                slider.value = volume;
              }}
              if (valueNode) {{
                valueNode.textContent = volume + "%";
              }}
              lastSentVolume = volume;
            }}
          }} catch (_error) {{
            // Keep the remote usable even if polling fails.
          }} finally {{
            refreshInFlight = false;
          }}
        }}
        function toggleOptionsPanel(forceOpen) {{
          const panel = document.getElementById("remoteOptionsPanel");
          if (!panel) return;
          const open = typeof forceOpen === "boolean" ? forceOpen : panel.hasAttribute("hidden");
          if (open) {{
            panel.removeAttribute("hidden");
          }} else {{
            panel.setAttribute("hidden", "");
          }}
          document.body.classList.toggle("options-open", open);
          window.sessionStorage.setItem("ksoundRemoteOptionsOpen", open ? "1" : "0");
          document.getElementById("optionsButton").setAttribute("aria-expanded", String(open));
        }}

        function openFolderOptions() {{
          toggleOptionsPanel(true);
          document.getElementById("folderStyleSelect").focus({{ preventScroll: true }});
        }}

        document.addEventListener("keydown", event => {{
          if (event.key === "Escape") toggleOptionsPanel(false);
        }});

        function restoreOptionsPanelState() {{
          const panel = document.getElementById("remoteOptionsPanel");
          if (!panel) return;
          const open = window.sessionStorage.getItem("ksoundRemoteOptionsOpen") === "1";
          if (open) {{
            panel.removeAttribute("hidden");
            document.body.classList.add("options-open");
          }}
        }}

        function saveRemoteOption(key, value) {{
          setRemoteStatus("Saving option...");
          fetch("/settings?token={escaped_token}", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/x-www-form-urlencoded",
              "X-Requested-With": "fetch"
            }},
            body: "key=" + encodeURIComponent(key) + "&value=" + encodeURIComponent(value)
          }})
          .then(response => {{
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
          }})
          .then(data => {{
            applyRemoteOption(key, data && data[key] !== undefined ? data[key] : value);
            setRemoteStatus("Option saved");
          }})
          .catch(() => {{
            setRemoteStatus("Option save error");
          }});
        }}

        function applyRemoteOption(key, value) {{
          const clean = String(value || "").trim().toLowerCase();
          if (key === "remote_pinch_outside_edit") {{
            allowPinchOutsideEdit = value === true || ["1", "true", "yes", "on"].includes(clean);
            const checkbox = document.getElementById("pinchOutsideEditOption");
            if (checkbox) checkbox.checked = allowPinchOutsideEdit;
            hardResetGesture();
            applyGestureModeVisuals();
            const hint = document.getElementById("remoteGestureHint");
            if (hint) hint.textContent = editMode ? EDIT_HINT : playHint();
            setRemoteStatus(editMode ? EDIT_HINT : playHint());
            return;
          }}
          const prefixes = {{
            remote_contrast: "contrast-",
            remote_background: "remote-bg-",
            remote_folder_size: "folder-size-",
            remote_folder_style: "folder-style-"
          }};
          const prefix = prefixes[key];
          if (!prefix || !clean) return;
          Array.from(document.body.classList).forEach(name => {{
            if (name.startsWith(prefix)) document.body.classList.remove(name);
          }});
          document.body.classList.add(prefix + clean);
        }}

        function stopAllSounds() {{
          postAction("/stop", "", "Stop all sent");
        }}

        function scheduleVolumeUpdate(value) {{
          document.getElementById("globalVolValue").textContent = value + "%";
          setVolumeState("Pending...");
          if (volumeTimer) clearTimeout(volumeTimer);
          volumeTimer = setTimeout(() => sendVolumeNow(value), 180);
        }}

        function sendVolumeNow(value) {{
          document.getElementById("globalVolValue").textContent = value + "%";
          if (volumeTimer) {{
            clearTimeout(volumeTimer);
            volumeTimer = null;
          }}
          if (String(value) === String(lastSentVolume)) {{
            setVolumeState("Ready");
            return;
          }}
          lastSentVolume = String(value);
          setVolumeState("Sending...");

          fetch("/volume?token={escaped_token}", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/x-www-form-urlencoded",
              "X-Requested-With": "fetch"
            }},
            body: "volume=" + encodeURIComponent(value)
          }})
          .then(response => {{
            if (!response.ok) throw new Error("HTTP " + response.status);
            setVolumeState("Saved");
          }})
          .catch(() => {{
            setVolumeState("Send error");
          }});
        }}

        const volumeSlider = document.getElementById("globalVolumeSlider");
        if (volumeSlider) {{
          volumeSlider.addEventListener("pointerdown", () => {{ volumeDragging = true; }});
          volumeSlider.addEventListener("pointerup", () => {{
            volumeDragging = false;
            refreshSettingsFromServer();
          }});
          volumeSlider.addEventListener("touchend", () => {{
            volumeDragging = false;
            refreshSettingsFromServer();
          }});
        }}

        applyFavoriteLayout();
        const nativeFloating = document.getElementById("floatingFavoritesButton");
        // Android IP15d has a manual overlay but not the IP15e automatic lifecycle.
        // Keep its working detach action until the tablet has actually updated its APK.
        const androidRemote = !!apkMatch;
        const androidEdgeFavorites = androidRemote && apkMatch[1].startsWith("IP15e");
        if (nativeFloating) {{
          nativeFloating.hidden = !androidRemote || androidEdgeFavorites;
          nativeFloating.setAttribute("aria-hidden", nativeFloating.hidden ? "true" : "false");
        }}
        document.getElementById("floatingAutoRow").hidden = !androidEdgeFavorites;
        document.getElementById("floatingAutoHint").hidden = !androidEdgeFavorites;
        window.addEventListener("resize", applyFavoriteLayout, {{passive:true}});
        let storedFolder = window.localStorage.getItem("ksoundSoundboardFolderV2");
        let activeFolder = storedFolder === null ? "__all__" : storedFolder;

        function normalizeFolderValue(value) {{
          return String(value || "").trim().toLowerCase();
        }}

        function currentVisiblePadCount() {{
          return visiblePadWraps.length;
        }}

        function updateVisiblePadDensity() {{
          const count = currentVisiblePadCount();
          document.body.classList.toggle("visible-one", count === 1);
          document.body.classList.toggle("visible-few", count > 0 && count <= 2);
        }}

        function applyFolderFilter() {{
          const active = normalizeFolderValue(activeFolder);
          const showAll = active === "__all__";

          document.querySelectorAll(".folder-pill").forEach(button => {{
            const rawValue = button.dataset.folder || "";
            const value = normalizeFolderValue(rawValue);
            const isAllButton = value === "__all__";

            button.classList.toggle("active", showAll ? isAllButton : value === active);
          }});

          if (!folderNodeIndex.size) rebuildFolderNodeIndex();
          const next = showAll
            ? (folderNodeIndex.get("__all__") || [])
            : (folderNodeIndex.get(active) || []);
          const nextSet = new Set(next);

          if (!folderFilterInitialized) {{
            (folderNodeIndex.get("__all__") || []).forEach(wrap => {{
              wrap.hidden = !nextSet.has(wrap);
            }});
            folderFilterInitialized = true;
          }} else {{
            visiblePadWraps.forEach(wrap => {{
              if (!nextSet.has(wrap)) wrap.hidden = true;
            }});
            next.forEach(wrap => {{ wrap.hidden = false; }});
          }}
          visiblePadWraps = next;
          next.forEach(observePadBackground);

          updateVisiblePadDensity();

          const count = currentVisiblePadCount();
          if (count === 0) {{
            setRemoteStatus("No sounds in this folder");
          }} else if (editMode) {{
            setRemoteStatus(EDIT_HINT);
          }} else {{
            setRemoteStatus("Ready");
          }}
        }}

        function selectFolder(value) {{
          activeFolder = String(value || "").trim();
          window.localStorage.setItem("ksoundSoundboardFolderV2", activeFolder);
          applyFolderFilter();

          updateSoundSelection();
          const label = activeFolder === "__all__" ? "All" : (activeFolder || "Main");
          setRemoteStatus("Folder: " + label);
        }}

        function initFolderButtons() {{
          document.querySelectorAll(".folder-pill").forEach(bindFolderButton);
        }}

        refreshPadIndexes();
        initFolderButtons();
        rebuildFolderNodeIndex();
        applyFolderFilter();
        restoreOptionsPanelState();
        if (currentColumns) {{
          applyColumns(currentColumns);
        }} else {{
          applyPadVisualScale(estimateColumns());
        }}
        document.getElementById("folderMoveDialog").addEventListener("cancel", event => {{
          if (selectionMovePending) event.preventDefault();
        }});
        initDragReorder();
        setEditMode(editMode, false);
        setRemoteMode(remoteMode, false);
        restoreScrollPositionAfterReload();
        window.setInterval(refreshSettingsFromServer, 900);
        window.setTimeout(refreshSettingsFromServer, 120);
      </script>
    </body>
    </html>
    """

    script = MODULE_JS.read_text(
        encoding="utf-8"
    ).replace(
        "__KSH_TOKEN_JSON__",
        json.dumps(TOKEN),
    )

    html_module = (
        "<style>"
        + MODULE_CSS
        + "</style><script>"
        + script
        + "</script>"
    )

    return body.replace(
        "</body>",
        html_module + "</body>",
    ).encode("utf-8")






def floating_page() -> bytes:
    """Responsive Android edge drawer; same token, favorites, pad art and Voice FX API."""
    js_token = json.dumps(TOKEN)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>K-Sounds edge favorites</title><style>
:root {{color-scheme:dark;--cyan:#3ed8ff;--muted:#9aadc2;--text:#eef7ff;}}
* {{box-sizing:border-box;}}
html,body {{height:100%;}}
body {{margin:0;overflow:hidden;background:linear-gradient(160deg,#101a2b,#070b14 72%);color:var(--text);font:13px system-ui,sans-serif;}}
.drawer {{height:100%;display:flex;flex-direction:column;overflow:hidden;border:1px solid #33465d;border-radius:17px;}}
header {{display:flex;align-items:center;gap:9px;padding:13px 12px 10px;flex-shrink:0;}}
.logo {{display:grid;place-items:center;width:31px;height:31px;border-radius:11px;color:#111928;font-weight:900;
 background:linear-gradient(135deg,#58e8ff,#9d8dff);box-shadow:0 2px 15px #3ed8ff35;}}
.heading {{min-width:0;flex:1;}}
.heading strong {{display:block;font-size:14px;letter-spacing:.02em;}}
.heading small {{display:block;margin-top:2px;color:var(--muted);font-size:11px;}}
header a {{display:grid;place-items:center;width:34px;height:34px;border:1px solid #3b5169;border-radius:11px;
 color:var(--text);text-decoration:none;font-size:21px;background:#172538;}}
.filters {{display:flex;gap:6px;padding:0 11px 10px;flex-shrink:0;}}
.filters button {{border:1px solid #334a62;border-radius:999px;background:#142135;color:#a8c1d6;
 padding:7px 11px;font:600 11px system-ui;}}
.filters button[aria-pressed="true"] {{background:#174354;border-color:#3ed8ff;color:#e6faff;}}
#status {{padding:0 12px 7px;min-height:20px;color:var(--muted);font-size:11px;flex-shrink:0;}}
#tiles {{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
 display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-content:start;gap:8px;padding:5px 11px 15px;}}
.tile {{position:relative;isolation:isolate;min-width:0;min-height:88px;overflow:hidden;border:1px solid #38546b;
 border-radius:13px;padding:9px;display:flex;align-items:flex-start;justify-content:space-between;flex-direction:column;
 text-align:left;font:inherit;color:var(--text);background-color:#18283e;
 background-image:linear-gradient(to bottom,rgba(4,9,18,.09),rgba(4,9,18,.85)),var(--art,linear-gradient(135deg,var(--a,#18354d),var(--b,#37244e)));
 background-size:cover;background-position:center;touch-action:manipulation;}}
.tile:active {{transform:scale(.985);}}
.tile.fx {{background-image:linear-gradient(130deg,#1b314a,#36253e);}}
.tile.fx.active {{border-color:#43dfff;box-shadow:inset 0 0 0 1px #43dfff;}}
.kind {{font-size:9px;letter-spacing:.09em;font-weight:850;color:#eaf9ff;padding:3px 5px;border-radius:5px;
 background:#050b16b0;border:1px solid #ffffff30;}}
.label {{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere;
 font-size:12px;line-height:1.19;font-weight:750;text-shadow:0 1px 6px #000;}}
.tile:disabled {{opacity:.42;}} .empty {{grid-column:1/-1;color:var(--muted);padding:12px 4px;line-height:1.5;}}
</style></head><body><div class="drawer">
<header><span class="logo">K</span><div class="heading"><strong>Quick favorites</strong><small id="count">Pads + Voice FX</small></div>
<a href="ksounds://floating-close" aria-label="Collapse side panel">×</a></header>
<nav class="filters" aria-label="Favorite type"><button data-filter="all" aria-pressed="true">All</button>
<button data-filter="pad" aria-pressed="false">Pads</button>
<button data-filter="voice_fx" aria-pressed="false">Voice FX</button></nav>
<div id="status" role="status">Connecting…</div><main id="tiles"></main></div>
<script>
const token={js_token};
const prefix="?token="+encodeURIComponent(token);
const tiles=document.getElementById("tiles");
const status=document.getElementById("status");
let filter="all",last="",busy=false,latest=null;
document.querySelectorAll("[data-filter]").forEach(button=>button.addEventListener("click",()=>{{
 filter=button.dataset.filter;
 document.querySelectorAll("[data-filter]").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));
 last="";if(latest)render(latest);
}}));
async function action(item,active) {{
 if(busy)return;
 busy=true;status.textContent="Sending…";
 try {{
  const path=item.type==="pad"?"/play":"/voice-fx";
  const body=item.type==="pad"?"slot="+encodeURIComponent(item.id):
    "effect="+encodeURIComponent(item.id)+"&enabled="+(!active);
  const response=await fetch(path+prefix,{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"fetch"}},body}});
  if(!response.ok)throw Error("Command failed");
  if(item.type==="voice_fx"){{const result=await response.json();if(!result.ok)throw Error("FX unavailable");}}
  status.textContent="Sent";last="";await refresh();
 }}catch(_e){{status.textContent="PC unavailable";}}
 finally{{busy=false;}}
}}
function render(data) {{
 const favorites=Array.isArray(data.favorites)?data.favorites:[];
 const slots=Array.isArray(data.slots)?data.slots:[];
 const effects=Array.isArray(data.voice_fx?.effects)?data.voice_fx.effects:[];
 const active=new Set(Array.isArray(data.voice_fx?.active)?data.voice_fx.active:[]);
 const signature=JSON.stringify([filter,favorites,slots.map(s=>[s.id,s.label,s.has_sound,s.auto_colors,s.has_background,s.background_version]),
   effects.map(e=>[e.id,e.label]),[...active]]);
 document.getElementById("count").textContent=favorites.length+" saved";
 if(signature===last)return;
 last=signature;
 const top=tiles.scrollTop;tiles.replaceChildren();
 for(const item of favorites) {{
  if(filter!=="all" && item.type!==filter)continue;
  const meta=item.type==="pad"?slots.find(s=>s.id===item.id):effects.find(e=>e.id===item.id);
  const button=document.createElement("button");button.type="button";
  button.className="tile"+(item.type==="voice_fx"?" fx":"");
  if(item.type==="pad" && meta){{
   const colors=Array.isArray(meta.auto_colors)?meta.auto_colors:[];
   if(colors[0])button.style.setProperty("--a",String(colors[0]));
   if(colors[1])button.style.setProperty("--b",String(colors[1]));
   if(meta.has_background && meta.background_version){{
    const art="/background"+prefix+"&slot="+encodeURIComponent(String(meta.id||""))+
      "&rev="+encodeURIComponent(String(meta.background_version));
    button.style.setProperty("--art",'url("'+art.replace(/"/g,"%22")+'")');
   }}
  }}
  const tag=document.createElement("span");tag.className="kind";tag.textContent=item.type==="pad"?"PAD":"FX";
  const name=document.createElement("span");name.className="label";name.textContent=String(meta?.label||item.id);
  button.append(tag,name);
  const isActive=active.has(item.id);
  if(item.type==="voice_fx")button.classList.toggle("active",isActive);
  button.disabled=!meta||(item.type==="pad"&&!meta.has_sound);
  button.addEventListener("click",()=>action(item,isActive));
  tiles.append(button);
 }}
 if(!tiles.children.length){{const empty=document.createElement("p");empty.className="empty";
  empty.textContent=favorites.length?"No favorites of this type.":"Pin pads and Voice FX in K-Sounds Remote.";tiles.append(empty);}}
 tiles.scrollTop=top;
}}
async function refresh() {{
 try{{
  const response=await fetch("/state"+prefix,{{cache:"no-store"}});
  if(!response.ok)throw Error("Disconnected");
  latest=await response.json();status.textContent="Connected";render(latest);
 }}catch(_e){{status.textContent="PC disconnected — waiting…";last="";
  tiles.querySelectorAll("button").forEach(button=>button.disabled=true);}}
}}
refresh();setInterval(refresh,1800);
</script></body></html>'''.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        return super().end_headers()

    def _token_ok(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest(query.get("token", [""])[0].encode("utf-8"), TOKEN.encode("utf-8"))

    def _send_page(self, status: str = "") -> None:
        data = page(status)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not _remote_client_allowed(self.client_address[0]):
            json_response(self, 403, {"ok": False, "error": "LAN access only"})
            return
        parsed = urlparse(self.path)

        # Local lifecycle probes must not require the pairing token and must
        # not count as user activity for the optional idle policy.
        if parsed.path == "/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "k-sounds-soundboard-remote",
                    "version": 1,
                    "remote_build": "IP10t",
                    "hub_build": "IP14b",
                    "surfaces": list(REMOTE_SURFACES),
                },
            )
            return

        mark_remote_activity(parsed.path)

        # Pairing must be reachable without the normal web token.
        # Always answer JSON so the Android app never tries to parse plain
        # "Forbidden" as a JSONObject.
        if parsed.path == "/api/pair":
            if not _pairing_rate_limit_allows(self.client_address[0]):
                json_response(self, 429, {"ok": False, "error": "Pairing attempt limit reached"})
                return
            query = parse_qs(parsed.query)
            pin = query.get("pin", [""])[0].strip()

            if not pin:
                json_response(self, 400, {"ok": False, "error": "Missing pairing code"})
                return

            if consume_pairing_if_pin_matches(pin):
                json_response(self, 200, {"ok": True, "token": TOKEN})
                return

            json_response(self, 403, {"ok": False, "error": "Invalid or expired pairing code"})
            return

        if not self._token_ok():
            json_response(self, 403, {"ok": False, "error": "Forbidden"})
            return

        if parsed.path == "/floating":
            data = floating_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/background":
            if not remote_surface_enabled("pads"):
                json_response(self, 404, {"ok": False, "error": "Pads module disabled"})
                return
            query = parse_qs(parsed.query)
            slot_id = query.get("slot", [""])[0].strip()
            slot = next((item for item in read_slots() if str(item.get("id", "")) == slot_id), None)

            if slot is None:
                json_response(self, 404, {"ok": False, "error": "Background slot not found"})
                return

            image_path = Path(str(slot.get("background_path") or "")).expanduser()
            if not image_path.is_file():
                json_response(self, 404, {"ok": False, "error": "Background not found"})
                return

            mime_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
            if not mime_type.startswith("image/"):
                json_response(self, 415, {"ok": False, "error": "Unsupported background type"})
                return

            background_version = background_file_version(str(image_path))
            etag = '"background-' + hashlib.sha256(
                f"{slot_id}:{background_version}".encode("utf-8", "ignore")
            ).hexdigest() + '"'
            if str(self.headers.get("If-None-Match") or "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, max-age=31536000, immutable")
                self.end_headers()
                return

            try:
                data = image_path.read_bytes()
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
                return

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/state":
            conditional_json_response(self, read_soundboard_state())
            return

        if parsed.path == "/module-action-result":
            ident = parse_qs(
                parsed.query
            ).get(
                "request_id",
                [""],
            )[0]

            receipt = receipt_read(
                MODULE_RECEIPTS_PATH,
                ident,
            )

            json_response(
                self,
                200 if receipt is not None else 404,
                {
                    "ok": receipt is not None,
                    **(
                        receipt
                        or {
                            "error": "Unknown or expired request"
                        }
                    ),
                },
            )
            return

        if parsed.path == "/module-extensions":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "surfaces": read_remote_modules(
                        MODULE_REMOTE_PATH
                    ),
                },
            )
            return

        if parsed.path == "/settings":
            json_response(self, 200, read_soundboard_settings())
            return

        self._send_page()


    def do_POST(self) -> None:
        if not _remote_client_allowed(self.client_address[0]):
            json_response(self, 403, {"ok": False, "error": "LAN access only"})
            return
        mark_remote_activity("POST")
        if not self._token_ok():
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if not 0 <= length <= 1048576:
            json_response(self, 413, {"ok": False, "error": "Invalid request size"})
            return
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        form = parse_qs(raw)

        parsed = urlparse(self.path)
        if parsed.path == "/module-action":
            try:
                request = json.loads(
                    form.get(
                        "payload",
                        ["{}"],
                    )[0]
                )

                if not isinstance(request, dict):
                    raise ValueError(
                        "Expected object"
                    )

                mid, sid, cid = (
                    request.get(key)
                    for key in (
                        "module",
                        "surface",
                        "control",
                    )
                )

                if not all(
                    isinstance(value, str)
                    and 0 < len(value) <= 64
                    for value in (
                        mid,
                        sid,
                        cid,
                    )
                ):
                    raise ValueError(
                        "Invalid identity"
                    )

                control = control_for(
                    read_remote_modules(
                        MODULE_REMOTE_PATH
                    ),
                    mid,
                    sid,
                    cid,
                )

                if (
                    control is None
                    or not valid_value(
                        control,
                        request.get("value"),
                    )
                ):
                    raise ValueError(
                        "Module unavailable or invalid value"
                    )

                event = {
                    "command": "external-module-ui-action",
                    "module": mid,
                    "surface": sid,
                    "control": cid,
                    "module_action": control["action"],
                    "value": request["value"],
                }

            except (
                TypeError,
                ValueError,
                KeyError,
            ) as exc:
                json_response(
                    self,
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )
                return

            try:
                ident = receipt_begin(MODULE_RECEIPTS_PATH)
            except OSError:
                json_response(
                    self,
                    503,
                    {"ok": False, "error": "Receipt unavailable"},
                )
                return
            event["request_id"] = ident
            ok, _ = send_ipc(event)
            if not ok:
                receipt_finish(
                    MODULE_RECEIPTS_PATH,
                    ident,
                    False,
                    "K-Sounds unavailable",
                )
            json_response(
                self,
                202 if ok else 503,
                {
                    "ok": ok,
                    "queued": ok,"request_id":ident,
                    "error": (
                        ""
                        if ok
                        else "K-Sounds unavailable"
                    ),
                },
            )
            return

        if parsed.path == "/favorites":
            try:
                requested = json.loads(form.get("items", ["[]"])[0])
                if not isinstance(requested, list) or len(requested) > MAX_REMOTE_FAVORITES:
                    raise ValueError("Favorites request too large")
                if len(normalize_remote_favorites(requested)) != len(requested):
                    raise ValueError("Invalid or repeated favorite")
                if any((item["type"] == "pad" and not remote_surface_enabled("pads"))
                       or (item["type"] == "voice_fx" and not remote_surface_enabled("voice_fx"))
                       for item in requested):
                    raise ValueError("Favorite module disabled")
                favorites = save_remote_favorites(requested)
            except (ValueError, TypeError) as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
                return
            json_response(self, 200, {"ok": True, "favorites": favorites})
            return

        if parsed.path == "/voice-fx":
            if not remote_surface_enabled("voice_fx"):
                json_response(self, 404, {"ok": False, "error": "Voice FX module disabled"})
                return
            effect = str(form.get("effect", [""])[0] or "").strip().lower()
            if effect not in VOICE_FX_ORDER:
                json_response(self, 400, {"ok": False, "error": "Unknown Voice FX"})
                return
            enabled_raw = str(form.get("enabled", ["true"])[0] or "").strip().lower()
            enabled = enabled_raw in {"1", "true", "yes", "on"}
            ok, _message = send_ipc({"command": "voice-fx-toggle", "effect": effect, "enabled": enabled})
            if not ok:
                json_response(self, 503, {"ok": False, "error": "K-Sounds Hub is unavailable"})
                return
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                state = read_voice_fx_state()
                if (effect in set(state.get("active", []))) == enabled:
                    json_response(self, 200, {"ok": True, "voice_fx": state})
                    return
                time.sleep(0.03)
            json_response(self, 409, {"ok": False, "error": "Voice FX state was not confirmed", "voice_fx": read_voice_fx_state()})
            return

        if not remote_surface_enabled("pads"):
            json_response(self, 404, {"ok": False, "error": "Pads module disabled"})
            return

        if parsed.path == "/move-to-folder":
            # Keep the standalone entry point usable from the installed app tree.
            source_root = str(Path(__file__).resolve().parents[2] / "src")
            if source_root not in sys.path:
                sys.path.insert(0, source_root)
            from ksound_hub.soundboard_library import remote_folder_move
            try:
                ids = json.loads(form.get("ids", ["[]"])[0])
                _, _, destination = remote_folder_move(
                    read_soundboard_document(), ids, form.get("folder", [""])[0],
                    expected_folder=form.get("remove_from", [None])[0])
            except (ValueError, TypeError) as exc:
                json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            ok, _ = send_ipc({"command": "soundboard-move-to-folder", "ids": ids,
                              "folder": destination, "expected_folder": form.get("remove_from", [None])[0]})
            if not ok:
                json_response(self, 503, {"ok": False, "error": "K-Sounds Hub is unavailable. Selection kept."})
                return
            deadline = time.monotonic() + 2.0
            wanted = set(ids)
            while time.monotonic() < deadline:
                matched = [slot for slot in read_slots() if slot.get("id") in wanted]
                if len(matched) == len(wanted) and all(
                        slot.get("folder", "") == destination for slot in matched):
                    json_response(self, 200, {"ok": True, "folder": destination,
                                             "state": read_soundboard_state()})
                    return
                time.sleep(0.04)
            json_response(self, 409, {"ok": False,
                "error": "Move not confirmed. Selection kept; reload to check before retrying."})
            return

        if parsed.path == "/play":
            slot = form.get("slot", [""])[0]
            ok, msg = send_ipc({"command": "soundboard-play", "slot": slot})
            self._send_page(f"Play {slot}: {msg if not ok else 'sent'}")
            return

        if parsed.path == "/stop":
            ok, msg = send_ipc({"command": "soundboard-stop-all"})
            self._send_page(f"Stop all: {msg if not ok else 'sent'}")
            return

        if parsed.path == "/reorder":
            try:
                order = json.loads(form.get("order", ["[]"])[0])
                current = [slot["id"] for slot in read_soundboard_state()["slots"]]
                if (not isinstance(order, list) or not order
                        or any(not isinstance(value, str) for value in order)
                        or len(order) != len(current) or len(set(order)) != len(order)
                        or set(order) != set(current)):
                    raise ValueError("Soundboard changed. Reload before reordering.")
            except (ValueError, TypeError) as exc:
                json_response(self, 409, {"ok": False, "error": str(exc)})
                return
            ok, _ = send_ipc({"command": "soundboard-reorder-slots", "order": order})
            if not ok:
                json_response(self, 503, {"ok": False})
                return
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if [slot["id"] for slot in read_soundboard_state()["slots"]] == order:
                    json_response(self, 200, {"ok": True})
                    return
                time.sleep(0.04)
            json_response(self, 409, {"ok": False, "error": "Order not confirmed."})
            return

        if parsed.path == "/move":
            slot = form.get("slot", [""])[0]
            direction = form.get("direction", [""])[0]
            ok, msg = send_ipc({
                "command": "soundboard-move-slot",
                "slot": slot,
                "direction": direction,
            })
            self._send_page(f"Move {slot}: {msg if not ok else 'sent'}")
            return

        if parsed.path == "/settings":
            key = form.get("key", [""])[0]
            value = form.get("value", [""])[0]
            update_soundboard_setting(key, value)
            settings = read_soundboard_settings()
            json_response(self, 200, {"ok": True, **settings})
            return

        if parsed.path == "/volume":
            raw_volume = form.get("volume", ["100"])[0]
            try:
                volume = max(0, min(100, int(raw_volume)))
            except Exception:
                volume = 100

            update_soundboard_setting("global_volume", volume)

            ok, msg = send_ipc({"command": "soundboard-set-global-volume", "volume": volume})
            self._send_page(f"Global volume {volume}%: {msg if not ok else 'sent'}")
            return

        if parsed.path == "/layout":
            raw_columns = form.get("columns", ["0"])[0]
            try:
                columns = max(1, min(13, int(raw_columns)))
            except Exception:
                json_response(self, 400, {"ok": False, "error": "Invalid columns"})
                return

            update_soundboard_setting("remote_columns", columns)
            json_response(self, 200, {"ok": True, "remote_columns": columns})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


def discovery_loop(stop_event: threading.Event) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            print(f"Discovery UDP disabled: {exc}", file=sys.stderr, flush=True)
            return

        sock.settimeout(1.0)
        print(f"Discovery UDP listening on 0.0.0.0:{DISCOVERY_PORT}", flush=True)

        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            if data.strip() not in DISCOVERY_REQUESTS:
                continue

            payload = {
                "service": DISCOVERY_SERVICE,
                "version": 2,
                "name": SERVICE_NAME,
                "http_port": HTTP_PORT,
                "pairing_available": read_valid_pairing() is not None,
            }
            try:
                sock.sendto(json.dumps(payload).encode("utf-8"), addr)
            except OSError:
                pass


def start_idle_shutdown_watch(server: ThreadingHTTPServer) -> None:
    if REMOTE_IDLE_TIMEOUT_SECONDS <= 0:
        print("Remote idle auto-stop disabled", flush=True)
        return

    def run() -> None:
        while True:
            time.sleep(REMOTE_IDLE_CHECK_SECONDS)
            with _REMOTE_ACTIVITY_LOCK:
                idle_for = time.monotonic() - _REMOTE_LAST_ACTIVITY

            if idle_for >= REMOTE_IDLE_TIMEOUT_SECONDS:
                print(
                    f"Remote idle for {int(idle_for)}s; stopping HTTP/discovery server",
                    flush=True,
                )
                server.shutdown()
                return

    thread = threading.Thread(target=run, name="soundboard-remote-idle-stop", daemon=True)
    thread.start()


def main() -> int:
    ip = local_ip()
    stop_event = threading.Event()
    thread = threading.Thread(target=discovery_loop, args=(stop_event,), daemon=True)
    thread.start()

    print()
    print("K-Sounds Soundboard Remote")
    print("==========================")
    print(f"HTTP     : http://127.0.0.1:{HTTP_PORT}/")
    print(f"LAN      : http://{ip}:{HTTP_PORT}/")
    print(f"Discovery: UDP {DISCOVERY_PORT}")
    print()
    print("Pairing:")
    print("  ksound-soundboard-pair")
    print()
    print("Ctrl+C pour arrêter.")
    print()

    server = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    start_idle_shutdown_watch(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
