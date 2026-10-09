#!/usr/bin/env bash
set -Eeuo pipefail

# Stop only processes owned by the current user and identifiable as K-Sounds.
# Runtime roles are the primary ownership marker. The command markers cover
# older K-Sounds versions that predate KSH_RUNTIME_ROLE.
python3 - <<'PY'
from __future__ import annotations

import os
import signal
import time
from pathlib import Path


TARGET_ROLES = {
    "eq_slot",
    "meter_probe",
    "native_meter_engine",
    "native_engine_shadow",
    "native_engine_bridge",
    "native_engine_real",
    "native_micro_engine",
    "v2_final_capture",
    "v2_final_render",
    "soundboard_remote_web",
    "hud_overlay_renderer",
    "glass_device_watch",
}

COMMAND_MARKERS = (
    " -m ksound_hub.glass_app",
    " -m ksound_hub.app",
    "/ksound_hub/glass_app.py",
    "/ksound_hub/app.py",
    "ksound_soundboard_web.py",
    "ksound_native_engine",
    "ksound_native_micro_engine",
    "ksound_native_meter_engine",
    "audio-hud-overlay",
)

proc_root = Path("/proc")
uid = os.getuid()
own_pid = os.getpid()
parent_pid = os.getppid()
targets: list[int] = []


def real_uid(entry: Path) -> int | None:
    try:
        text = (entry / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            return int(parts[1]) if len(parts) >= 2 else None
    return None


def process_env(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def command_line(entry: Path) -> str:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")


for entry in proc_root.iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    if pid in {own_pid, parent_pid} or real_uid(entry) != uid:
        continue

    env = process_env(entry)
    cmdline = command_line(entry)
    if env.get("KSH_RUNTIME_ROLE") in TARGET_ROLES or any(
        marker in cmdline for marker in COMMAND_MARKERS
    ):
        targets.append(pid)

targets = list(dict.fromkeys(targets))
for pid in targets:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

deadline = time.monotonic() + 2.0
remaining = set(targets)
while remaining and time.monotonic() < deadline:
    remaining = {pid for pid in remaining if (proc_root / str(pid)).exists()}
    if remaining:
        time.sleep(0.05)

for pid in remaining:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

print(f"K-Sounds runtime processes stopped: {len(targets)}")
PY
