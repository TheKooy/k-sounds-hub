#!/usr/bin/env python3
from __future__ import annotations

import json

from ksound_hub.shortcuts_portal_runtime import XdgGlobalShortcutsRuntime


def safe_shape(value):
    if isinstance(value, dict):
        return {str(k): safe_shape(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_shape(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    current_signature = getattr(value, "currentSignature", None)
    if callable(current_signature):
        try:
            return {
                "_type": type(value).__name__,
                "_dbus_signature": str(current_signature()),
            }
        except Exception:
            pass
    return {"_type": type(value).__name__, "_repr": repr(value)[:240]}


def main() -> int:
    runtime = XdgGlobalShortcutsRuntime()
    payload = {
        "ok": False,
        "stage": "start",
        "app_id": runtime.identity.app_id,
    }
    try:
        runtime.create_session()
        payload["session_handle"] = runtime.session_handle
        payload["registry_registered"] = runtime.registry_registered

        listed = runtime.list_shortcuts()
        payload["list_response"] = listed.response
        payload["results"] = safe_shape(listed.results)

        if listed.response != 0:
            payload["stage"] = "list-response"
            payload["detail"] = f"ListShortcuts response={listed.response}"
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 3

        runtime.close()
        payload["session_closed"] = True
        payload["ok"] = True
        payload["stage"] = "complete"
        payload["detail"] = "CreateSession + ListShortcuts + Close succeeded"
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        payload["stage"] = "exception"
        payload["detail"] = f"{type(exc).__name__}: {exc}"
        try:
            runtime.close()
        except Exception:
            pass
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
