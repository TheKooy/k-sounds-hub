#!/usr/bin/env python3
"""Validate the public K-Sounds launch contract without importing Qt."""

from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"launch contract error: {message}")


def main() -> int:
    project = tomllib.loads(ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    entrypoints = project.get("project", {}).get("scripts", {})
    launcher = ROOT.joinpath("scripts/start_ksound_hub_v2.sh").read_text(encoding="utf-8")
    installer = ROOT.joinpath("install.sh").read_text(encoding="utf-8")
    migration = ROOT.joinpath("scripts/migrate_user_integration.sh").read_text(encoding="utf-8")

    for command in ("k-sounds-hub", "ksound-hub", "k-sounds-hub-glass", "ksound-hub-glass"):
        require(
            entrypoints.get(command) == "ksound_hub.glass_app:main",
            f"{command} must launch Glass",
        )

    require(
        entrypoints.get("k-sounds-hub-classic") == "ksound_hub.app:main",
        "the classic frontend must only have an explicit fallback command",
    )
    require(
        'FRONTEND_MODULE="${KSH_FRONTEND_MODULE:-ksound_hub.glass_app}"' in launcher,
        "the shell launcher must default to Glass",
    )
    require('exec "$PYTHON_BIN" -m "$FRONTEND_MODULE"' in launcher, "launcher exec is not canonical")
    require("start_easyeffects_service" not in launcher, "EasyEffects autostart function still exists")
    require("easyeffects --service-mode" not in launcher, "EasyEffects is still started automatically")
    require(
        'QT_QPA_PLATFORM="${KSH_QT_PLATFORM:-xcb}"' not in launcher,
        "the launcher still forces XWayland/XCB",
    )
    require(
        'export KSH_NATIVE_MIC="${KSH_NATIVE_MIC:-1}"' in launcher,
        "the launcher must default to the validated native Micro engine",
    )
    require(
        'restart --project-root "$REPO_DIR" --quiet' in launcher,
        "a cold canonical launch must restart the Remote from the current project tree",
    )
    require(
        'soundboard_remote_runtime \\\n    start --project-root "$REPO_DIR" --quiet' not in launcher,
        "the canonical launcher must not accept an already-running legacy Remote",
    )
    require('Exec=$WRAPPER_PATH' in installer, "desktop entry must use the canonical wrapper")
    require(
        'DESKTOP_COMPAT_PATH="$APPS_DIR/ksounds-hub.desktop"' in installer,
        "Qt/KDE desktop-id compatibility launcher is missing",
    )
    require(
        'DESKTOP_GLASS_COMPAT_PATH="$APPS_DIR/ksounds-hub-glass.desktop"' in installer,
        "legacy KDE favorite compatibility launcher is missing",
    )
    require(
        "KSH_FRONTEND_MODULE=ksound_hub.app" in migration,
        "installer must expose the classic fallback explicitly",
    )

    print("launch contract: Glass is canonical; classic is explicit; EasyEffects autostart is absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
