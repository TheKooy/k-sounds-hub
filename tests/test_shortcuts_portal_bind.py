from __future__ import annotations

import pytest

from ksound_hub.shortcuts_portal_bind import (
    XdgGlobalShortcut,
    bind_shortcuts_options,
    bind_shortcuts_payload,
    tune_shortcut_spec,
)


def test_tune_spec_uses_stable_action_id_and_xdg_trigger():
    spec = tune_shortcut_spec("Ctrl+Shift+B")
    assert spec.shortcut_id == "micro.tune.toggle"
    assert spec.description == "Toggle MICRO Processing (TUNE)"
    assert spec.preferred_trigger == "CTRL+SHIFT+B"


def test_bind_payload_matches_portal_shape():
    payload = bind_shortcuts_payload((tune_shortcut_spec("Ctrl+B"),))
    assert payload == [
        (
            "micro.tune.toggle",
            {
                "description": "Toggle MICRO Processing (TUNE)",
                "preferred_trigger": "CTRL+B",
            },
        )
    ]


def test_bind_options_keep_plain_string_token():
    options = bind_shortcuts_options("bind_123")
    assert options == {"handle_token": "bind_123"}
    assert type(options["handle_token"]) is str


def test_empty_bind_payload_is_rejected():
    with pytest.raises(ValueError):
        bind_shortcuts_payload(())


def test_empty_bind_token_is_rejected():
    with pytest.raises(ValueError):
        bind_shortcuts_options("")


def test_generic_bridge_accepts_product_action_identity():
    bridge = XdgGlobalShortcut(
        shortcut_id="mixer.game.mute",
        description="Toggle GAME mute",
        on_activate=lambda: None,
    )
    assert bridge.shortcut_id == "mixer.game.mute"
    assert bridge.description == "Toggle GAME mute"
    assert bridge.state().active is False


def test_bridge_sync_tracks_kde_enabled_disabled_and_trigger_changes():
    states = []
    activations = []
    bridge = XdgGlobalShortcut(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        on_activate=lambda: activations.append("on"),
        on_state_change=states.append,
    )

    bridge._handle_line("READY\tmicro.tune.toggle\tF23")
    bridge._handle_line("SYNC\tmicro.tune.toggle\tENABLED\tF23")
    assert bridge.portal_ready is True
    assert bridge.registered is True
    assert bridge.kde_enabled is True
    assert bridge.kde_trigger == "F23"
    assert states[-1].kde_trigger == "F23"

    bridge._handle_line("ACTIVATED\tmicro.tune.toggle")
    assert activations == ["on"]

    bridge._handle_line("SYNC\tmicro.tune.toggle\tENABLED\tF22")
    assert bridge.kde_trigger == "F22"
    assert states[-1].kde_trigger == "F22"

    bridge._handle_line("SYNC\tmicro.tune.toggle\tDISABLED\t")
    assert bridge.registered is True
    assert bridge.kde_enabled is False
    bridge._handle_line("ACTIVATED\tmicro.tune.toggle")
    assert activations == ["on"]


def test_bridge_missing_kde_action_is_authoritative_but_not_enabled():
    bridge = XdgGlobalShortcut(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        on_activate=lambda: None,
    )
    bridge._handle_line("READY\tmicro.tune.toggle\tF23")
    bridge._handle_line("SYNC\tmicro.tune.toggle\tMISSING\t")
    state = bridge.state()
    assert state.portal_ready is True
    assert state.registered is False
    assert state.kde_enabled is False
    assert state.active is False
