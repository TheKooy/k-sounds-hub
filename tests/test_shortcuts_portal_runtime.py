from __future__ import annotations

import pytest

from ksound_hub.shortcuts_portal_runtime import (
    PortalRuntimeSnapshot,
    list_shortcuts_options,
)


def test_list_shortcuts_options_keep_plain_string():
    options = list_shortcuts_options("list_123")
    assert options == {"handle_token": "list_123"}
    assert type(options["handle_token"]) is str


def test_list_shortcuts_options_reject_empty_token():
    with pytest.raises(ValueError):
        list_shortcuts_options("")


def test_runtime_snapshot_is_immutable_value_object():
    snapshot = PortalRuntimeSnapshot(
        ready=True,
        app_id="k-sounds-hub",
        session_handle="/session/test",
        detail="ready",
    )
    assert snapshot.ready is True
    assert snapshot.app_id == "k-sounds-hub"
    assert snapshot.session_handle == "/session/test"


def test_runtime_keeps_owned_qcoreapplication_slot():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "src/ksound_hub/shortcuts_portal_runtime.py"
    ).read_text(encoding="utf-8")
    assert "self._owned_core_app = None" in source
    assert "if app is None:" in source
    assert "self._owned_core_app = QCoreApplication([])" in source
