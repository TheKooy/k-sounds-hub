from __future__ import annotations

from pathlib import Path

from ksound_hub.desktop_integration import (
    CANONICAL_DESKTOP_ID,
    autostart_enabled,
    set_autostart_enabled,
)


def _write_entry(path: Path, *extra_lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(("[Desktop Entry]", "Type=Application", *extra_lines, "")),
        encoding="utf-8",
    )


def test_autostart_is_opt_in_and_tracks_the_real_xdg_entry(tmp_path: Path) -> None:
    autostart_dir = tmp_path / "config" / "autostart"
    wrapper = tmp_path / "bin" / "k-sounds-hub"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    assert not autostart_enabled(autostart_dir)
    assert set_autostart_enabled(True, directory=autostart_dir, wrapper=wrapper)

    canonical = autostart_dir / CANONICAL_DESKTOP_ID
    assert canonical.is_file()
    assert f"Exec={wrapper}" in canonical.read_text(encoding="utf-8")
    assert autostart_enabled(autostart_dir)

    assert not set_autostart_enabled(False, directory=autostart_dir, wrapper=wrapper)
    assert not canonical.exists()
    assert not autostart_enabled(autostart_dir)


def test_enabling_replaces_an_active_legacy_id(tmp_path: Path) -> None:
    autostart_dir = tmp_path / "autostart"
    legacy = autostart_dir / "ksounds-hub.desktop"
    wrapper = tmp_path / "k-sounds-hub"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    _write_entry(legacy, "Exec=/old/ksounds-hub-glass")

    assert autostart_enabled(autostart_dir)
    assert set_autostart_enabled(True, directory=autostart_dir, wrapper=wrapper)
    assert not legacy.exists()
    assert (autostart_dir / CANONICAL_DESKTOP_ID).is_file()


def test_hidden_or_explicitly_disabled_entries_are_not_active(tmp_path: Path) -> None:
    autostart_dir = tmp_path / "autostart"
    _write_entry(autostart_dir / CANONICAL_DESKTOP_ID, "Hidden=true", "Exec=/unused")
    assert not autostart_enabled(autostart_dir)

    _write_entry(
        autostart_dir / CANONICAL_DESKTOP_ID,
        "X-GNOME-Autostart-enabled=false",
        "Exec=/unused",
    )
    assert not autostart_enabled(autostart_dir)


def test_enabling_requires_the_canonical_wrapper_to_exist(tmp_path: Path) -> None:
    autostart_dir = tmp_path / "autostart"
    missing_wrapper = tmp_path / "missing" / "k-sounds-hub"

    try:
        set_autostart_enabled(True, directory=autostart_dir, wrapper=missing_wrapper)
    except FileNotFoundError as exc:
        assert str(missing_wrapper) in str(exc)
    else:
        raise AssertionError("enabling autostart accepted a missing launcher")

    assert not autostart_enabled(autostart_dir)
