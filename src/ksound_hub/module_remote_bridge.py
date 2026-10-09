"""JSON-only snapshots of active external Remote surfaces."""

from __future__ import annotations

import json
import os
import tempfile
import time

from pathlib import Path

FILE_NAME = "module_remote_surfaces.json"
MAX_AGE = 6


def ticks(pid):
    try:
        return (
            Path(f"/proc/{pid}/stat")
            .read_text()
            .rsplit(") ", 1)[1]
            .split()[19]
        )
    except (OSError, IndexError, ValueError):
        return ""


def publish(path, surfaces, confirmed=None):
    confirmed = confirmed if isinstance(confirmed, dict) else {}
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    data = {
        "schema": 1,
        "pid": os.getpid(),
        "ticks": ticks(os.getpid()),
        "expires": time.time() + MAX_AGE,
        "surfaces": [],
    }

    for surface in surfaces:
        data["surfaces"].append({
            "module": surface.module_id,
            "id": surface.surface_id,
            "label": surface.label,
            "icon": surface.icon,
            "controls": [
                {
                    "id": control.control_id,
                    "type": control.kind,
                    "label": control.label,
                    "action": control.action,
                    "min": control.minimum,
                    "max": control.maximum,
                    "default": control.initial,
                    "options": [
                        {
                            "label": label,
                            "value": value,
                        }
                        for label, value in control.options
                    ],
                }
                for control in surface.controls
            ],
        })

    for item in data["surfaces"]:
        item["state"] = confirmed.get(item["module"], {}).get(item["id"], {})
    fd, temporary = tempfile.mkstemp(
        prefix=".ksh-module-",
        dir=path.parent,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                data,
                handle,
                ensure_ascii=False,
            )
            handle.write("\n")

        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    finally:
        Path(temporary).unlink(
            missing_ok=True
        )


def read(path):
    try:
        if path.stat().st_size > 262144:
            return []

        data = json.loads(
            path.read_text(encoding="utf-8")
        )

        if (
            not isinstance(data, dict)
            or data.get("schema") != 1
        ):
            return []

        pid = data.get("pid")

        if type(pid) is not int or pid <= 0:
            return []

        current_ticks = ticks(pid)

        if (
            not current_ticks
            or current_ticks != data.get("ticks")
        ):
            return []

        if not time.time() < data.get("expires", 0):
            return []

        result = data.get("surfaces", [])

        if (
            isinstance(result, list)
            and len(result) <= 144
        ):
            return result

        return []

    except (OSError, TypeError, ValueError):
        return []


def control_for(items, mid, sid, cid):
    for surface in items:
        if (
            isinstance(surface, dict)
            and surface.get("module") == mid
            and surface.get("id") == sid
        ):
            for control in surface.get(
                "controls",
                [],
            ):
                if (
                    isinstance(control, dict)
                    and control.get("id") == cid
                    and control.get("type") != "label"
                ):
                    return control

    return None


def valid_value(control, value):
    kind = control["type"]

    if kind == "button":
        return value is True

    if kind == "toggle":
        return type(value) is bool

    if kind == "slider":
        return (
            type(value) is int
            and type(control.get("min")) is int
            and type(control.get("max")) is int
            and control["min"]
            <= value
            <= control["max"]
        )

    if kind == "select":
        return (
            isinstance(value, str)
            and value in [
                item.get("value")
                for item in control.get(
                    "options",
                    [],
                )
                if isinstance(item, dict)
            ]
        )

    return False

# IP15m4b3 — asynchronous module action receipts

import re
import secrets

RECEIPT_DIR = "module_remote_receipts"
RECEIPT_TTL = 90

_RECEIPT_ID = re.compile(r"[A-Za-z0-9_-]{20,64}\Z")


def _receipt_path(folder, ident):
    if (
        isinstance(ident, str)
        and _RECEIPT_ID.fullmatch(ident)
    ):
        return folder / (ident + ".json")

    return None


def _receipt_write(folder, ident, data):
    folder.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    fd, temporary = tempfile.mkstemp(
        prefix=".receipt-",
        dir=folder,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as output:
            json.dump(
                data,
                output,
                ensure_ascii=False,
            )

        os.chmod(temporary, 0o600)

        os.replace(
            temporary,
            _receipt_path(folder, ident),
        )

    finally:
        Path(temporary).unlink(
            missing_ok=True
        )


def receipt_begin(folder):
    now = time.time()

    folder.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    for old in folder.glob("*.json"):
        try:
            if (
                now - old.stat().st_mtime
                > RECEIPT_TTL
            ):
                old.unlink(
                    missing_ok=True
                )

        except OSError:
            pass

    ident = secrets.token_urlsafe(18)

    _receipt_write(
        folder,
        ident,
        {
            "state": "pending",
            "detail": "",
            "created": now,
        },
    )

    return ident


def receipt_read(folder, ident):
    path = _receipt_path(
        folder,
        ident,
    )

    if path is None:
        return None

    try:
        if path.stat().st_size > 2048:
            return None

        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if (
            not isinstance(data, dict)
            or data.get("state") not in (
                "pending",
                "accepted",
                "refused",
            )
            or type(data.get("created"))
            not in (int, float)
            or not 0 <= (
                time.time() - data["created"]
            ) < RECEIPT_TTL
            or not isinstance(
                data.get("detail"),
                str,
            )
        ):
            return None

        return {
            key: data[key]
            for key in (
                "state",
                "detail",
                "created",
            )
        }

    except (
        OSError,
        ValueError,
        TypeError,
    ):
        return None


def receipt_finish(
    folder,
    ident,
    accepted,
    message,
):
    previous = receipt_read(
        folder,
        ident,
    )

    if (
        previous is None
        or previous["state"] != "pending"
    ):
        return False

    _receipt_write(
        folder,
        ident,
        {
            "state": (
                "accepted"
                if accepted
                else "refused"
            ),
            "detail": str(message)[:180],
            "created": previous["created"],
        },
    )

    return True
