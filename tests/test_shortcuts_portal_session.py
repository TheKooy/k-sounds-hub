from __future__ import annotations

from pathlib import Path

import pytest

from ksound_hub.shortcuts_portal_session import (
    CANONICAL_APP_ID,
    CANONICAL_DESKTOP_ID,
    app_id_from_desktop_id,
    create_session_options,
    expected_request_path,
    portal_token,
    sender_path_component,
)


def test_canonical_app_id_matches_desktop_basename():
    assert app_id_from_desktop_id(CANONICAL_DESKTOP_ID) == CANONICAL_APP_ID
    assert CANONICAL_APP_ID == "k-sounds-hub"


def test_sender_path_component_matches_portal_convention():
    assert sender_path_component(":1.234") == "1_234"
    assert sender_path_component("org.example.App") == "org_example_App"


def test_expected_request_path_uses_sender_and_token():
    assert expected_request_path(":1.42", "create_abc") == (
        "/org/freedesktop/portal/desktop/request/1_42/create_abc"
    )


def test_portal_token_is_valid_object_path_element():
    token = portal_token("K-Sounds create")
    assert token.startswith("K_Sounds_create_")
    assert all(ch.isalnum() or ch == "_" for ch in token)


def test_portal_token_never_starts_with_digit():
    token = portal_token("12")
    assert token.startswith("k_12_")


def test_empty_desktop_id_is_rejected():
    with pytest.raises(ValueError):
        app_id_from_desktop_id("")


def test_create_session_options_keep_inner_values_as_plain_strings():
    options = create_session_options("request_token", "session_token")
    assert options == {
        "handle_token": "request_token",
        "session_handle_token": "session_token",
    }
    assert type(options["handle_token"]) is str
    assert type(options["session_handle_token"]) is str


def test_create_session_options_reject_empty_tokens():
    with pytest.raises(ValueError):
        create_session_options("", "session")
    with pytest.raises(ValueError):
        create_session_options("request", "")
