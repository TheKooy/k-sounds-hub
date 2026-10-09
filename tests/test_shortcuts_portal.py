from __future__ import annotations

import pytest

from ksound_hub.shortcuts import ShortcutBinding
from ksound_hub.shortcuts_portal import (
    portal_specs,
    qt_sequence_to_xdg_trigger,
)


@pytest.mark.parametrize(
    ("qt", "xdg"),
    [
        ("Ctrl+B", "CTRL+B"),
        ("Ctrl+Shift+B", "CTRL+SHIFT+B"),
        ("Alt+F15", "ALT+F15"),
        ("Meta+Space", "META+SPACE"),
        ("Super+F18", "META+F18"),
        ("Ctrl+PgUp", "CTRL+PAGEUP"),
    ],
)
def test_qt_sequence_to_xdg_trigger(qt, xdg):
    assert qt_sequence_to_xdg_trigger(qt) == xdg


def test_portal_specs_only_include_enabled_configured_bindings():
    bindings = (
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO processing",
            trigger="Ctrl+B",
            enabled=True,
            module="micro_processing",
        ),
        ShortcutBinding(
            shortcut_id="voice_fx.toggle",
            description="Toggle Voice FX",
            trigger="F15",
            enabled=False,
            module="voice_fx",
        ),
        ShortcutBinding(
            shortcut_id="mixer.game.mute",
            description="Mute GAME",
            trigger="",
            enabled=True,
            module="mixer",
        ),
    )

    specs = portal_specs(bindings)
    assert len(specs) == 1
    assert specs[0].shortcut_id == "micro.tune.toggle"
    assert specs[0].preferred_trigger == "CTRL+B"


def test_multi_chord_sequence_is_rejected_instead_of_changed_silently():
    with pytest.raises(ValueError):
        qt_sequence_to_xdg_trigger("Ctrl+K, Ctrl+B")


def test_modifiers_without_key_are_rejected():
    with pytest.raises(ValueError):
        qt_sequence_to_xdg_trigger("Ctrl+Shift")
