#!/usr/bin/env python3
from __future__ import annotations

import json

from ksound_hub.shortcuts_portal import probe_xdg_global_shortcuts_portal


def main() -> int:
    report = probe_xdg_global_shortcuts_portal()
    cap = report.capability
    payload = {
        "available": bool(cap.available),
        "backend": cap.backend,
        "version": int(cap.version),
        "session_bus_connected": bool(report.session_bus_connected),
        "host_registry_available": bool(report.host_registry_available),
        "detail": cap.detail,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if cap.available else 2


if __name__ == "__main__":
    raise SystemExit(main())
