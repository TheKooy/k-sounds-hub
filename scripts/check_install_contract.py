#!/usr/bin/env python3
"""Validate build/install/uninstall invariants without starting audio or Qt."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"install contract error: {message}")


def read(relative: str) -> str:
    return ROOT.joinpath(relative).read_text(encoding="utf-8")


def main() -> int:
    installer = read("install.sh")
    uninstaller = read("uninstall.sh")
    launcher = read("scripts/start_ksound_hub_v2.sh")
    native_cmake = read("native_engine/CMakeLists.txt")
    native_builder = read("scripts/build_ksound_native_engine.sh")
    hud_builder = read("scripts/build_hud_overlay.sh")
    hud_launcher = read("scripts/start_hud.sh")
    glass = read("src/ksound_hub/glass_app.py")
    runtime_stopper = read("scripts/stop_all_ksound_runtime.sh")
    integration_migration = read("scripts/migrate_user_integration.sh")
    desktop_integration = read("src/ksound_hub/desktop_integration.py")

    require("BUILD_NATIVE=1" in installer, "native runtime build must be enabled by default")
    require("BUILD_HUD=1" in installer, "HUD build must be attempted by default")
    require("--no-native" in installer, "native fallback install option is missing")
    require("--no-hud" in installer, "HUD opt-out is missing")
    require(
        'need_cmd flock || missing+=("flock")' in installer,
        "HUD lifecycle dependency flock is not checked",
    )
    require(
        'STAGING_APP_DIR="$APP_ROOT/.app-installing.$$"' in installer,
        "installer does not stage upgrades transactionally",
    )
    require(
        'PREVIOUS_APP_DIR="$APP_ROOT/.app-previous.$$"' in installer,
        "installer does not retain a rollback target during activation",
    )
    require(
        'mv -- "$STAGING_APP_DIR" "$FINAL_APP_DIR"' in installer,
        "installer never activates the validated staging tree",
    )
    require(
        'mv -- "$PREVIOUS_APP_DIR" "$FINAL_APP_DIR"' in installer,
        "installer does not restore the previous app after a failed activation",
    )
    require(
        'export KSH_NATIVE_MIC="${KSH_NATIVE_MIC:-1}"' in launcher,
        "native Micro must be the default launcher path",
    )
    require(
        'os.environ.get("KSH_NATIVE_PLAYBACK", "0")' in read(
            "src/ksound_hub/audio/pipewire_v2_final.py"
        ),
        "experimental native playback must remain disabled by default",
    )

    for component in (
        "ksound_native_engine",
        "ksound_native_micro_engine",
        "ksound_native_meter_engine",
    ):
        require(
            f"add_executable({component}" in native_cmake,
            f"CMake target is missing: {component}",
        )
        require(component in native_builder, f"native build does not validate: {component}")

    require("audio-hud-overlay" in hud_builder, "HUD build target is missing")
    require(
        all(action in hud_launcher for action in ("start)", "stop)", "status)")),
        "HUD launcher does not expose start/stop/status lifecycle actions",
    )
    require(
        "KSH_HUD_INSTANCE" in hud_launcher and "hud-overlay.pid" in hud_launcher,
        "HUD launcher does not scope ownership to its verified PID file",
    )
    require(
        "_hud_watchdog_timer" in glass
        and "_sync_overlay_watchdog" in glass
        and "_stop_overlay_renderer" in glass,
        "Glass does not supervise and stop the HUD renderer",
    )
    require(
        'rm -rf -- "$APP_ROOT"' not in uninstaller,
        "uninstaller would erase retired backups with the application root",
    )
    require("--purge-retired" in uninstaller, "retired backup purge must be explicit")
    require("ksound-soundboard-web.service" in uninstaller, "remote service cleanup is missing")
    require("audio-hud-overlay" in uninstaller, "HUD cleanup is missing")
    require("KSH_RUNTIME_ROLE" in runtime_stopper, "runtime stop must use ownership roles")
    require("pkill" not in runtime_stopper, "runtime stop must not use broad pkill patterns")
    require(
        'migrate_user_integration.sh" "$APP_DIR" --no-desktop' in installer,
        "installer does not use the canonical user integration migration",
    )
    require(
        "systemctl --user disable --now" in integration_migration,
        "old Remote systemd service is not disabled during migration",
    )
    require(
        "desktop.disabled-by-glass" in integration_migration,
        "misleading active autostart is not retired during migration",
    )
    require(
        "integration-$MIGRATION_STAMP" in integration_migration,
        "integration backups still use a patch-specific retirement path",
    )
    require(
        "existing_autostart_is_active && PRESERVE_AUTOSTART=1" in integration_migration,
        "upgrades do not preserve an existing active K-Sounds autostart",
    )
    require(
        'NoDisplay=true' in installer and 'ksounds-hub.desktop' in installer,
        "KDE desktop-id compatibility aliases are missing",
    )
    require(
        'CANONICAL_DESKTOP_ID = "k-sounds-hub.desktop"' in desktop_integration,
        "Settings autostart does not use the canonical desktop id",
    )
    require(
        'values.get("hidden", "false")' in desktop_integration
        and "x-gnome-autostart-enabled" in desktop_integration,
        "Settings autostart detection ignores desktop-entry disabled state",
    )

    print(
        "install contract: native components are reproducible; HUD is supervised; "
        "uninstall preserves user data by default; autostart remains opt-in"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
