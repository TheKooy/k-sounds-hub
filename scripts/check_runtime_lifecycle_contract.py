#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"runtime lifecycle contract failed: {message}")


def main() -> int:
    launcher = (ROOT / "scripts" / "start_ksound_hub_v2.sh").read_text(encoding="utf-8")
    glass = (ROOT / "src" / "ksound_hub" / "glass_app.py").read_text(encoding="utf-8")
    audio_runtime = (
        ROOT / "src" / "ksound_hub" / "audio" / "pipewire_v2_final.py"
    ).read_text(encoding="utf-8")
    manager = (ROOT / "src" / "ksound_hub" / "soundboard_remote_runtime.py").read_text(
        encoding="utf-8"
    )
    remote = (
        ROOT / "tools" / "soundboard_remote" / "ksound_soundboard_web.py"
    ).read_text(encoding="utf-8")

    require(
        "ksound_hub.soundboard_remote_runtime" in launcher,
        "the launcher does not use the canonical Remote manager",
    )
    # The shell launcher may describe a retired direct start path in comments.
    # Ignore full-line comments while rejecting actual direct-start commands.
    active_launcher = "\n".join(
        line for line in launcher.splitlines() if not line.lstrip().startswith("#")
    )
    require(
        "nohup env" not in active_launcher
        and "ksound_soundboard_web.py" not in active_launcher,
        "the launcher still owns a second direct Remote start path",
    )
    require(
        all(
            identifier not in launcher + glass + audio_runtime
            for identifier in ("RODE_Microphones", "SteelSeries_Arctis")
        ),
        "the active public runtime still contains personal hardware identifiers",
    )
    require(
        "from .soundboard_remote_runtime import" in glass,
        "Glass does not use the canonical Remote manager",
    )
    require(
        "stop_remote_server(" in glass and "ensure_remote_server(" in glass,
        "Glass start/stop is not routed through the Remote manager",
    )
    require(
        '["pkill"' not in glass,
        "Glass shutdown still contains broad pkill cleanup",
    )
    require(
        'KSH_RUNTIME_ROLE": RUNTIME_ROLE' in manager,
        "Remote child ownership is not recorded in its environment",
    )
    require(
        'connection.request("GET", "/health")' in manager,
        "Remote readiness is not validated through its health endpoint",
    )
    require(
        'if parsed.path == "/health"' in remote
        and '"service": "k-sounds-soundboard-remote"' in remote,
        "Remote health endpoint is missing or ambiguous",
    )
    print(
        "runtime lifecycle contract: one Remote manager, health-checked startup, "
        "owned shutdown, no broad Glass pkill"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
