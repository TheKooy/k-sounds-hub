from __future__ import annotations

from ksound_hub.shortcuts import (
    ShortcutBinding,
    ShortcutManager,
    ShortcutRegistry,
)


def test_binding_roundtrip_preserves_enabled_trigger_and_module():
    binding = ShortcutBinding(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO processing",
        trigger="Ctrl+B",
        enabled=False,
        module="micro_processing",
        allow_hold=True,
    )
    restored = ShortcutBinding.from_dict(binding.to_dict())
    assert restored == binding


def test_registry_dispatches_activation_once_and_hold_release():
    events: list[str] = []
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="voice_fx.hold",
            description="Hold Voice FX",
            trigger="F15",
            allow_hold=True,
        ),
        on_activate=lambda: events.append("on"),
        on_deactivate=lambda: events.append("off"),
    )

    assert registry.activate("voice_fx.hold") is True
    assert registry.activate("voice_fx.hold") is False
    assert events == ["on"]

    assert registry.deactivate("voice_fx.hold") is True
    assert events == ["on", "off"]


def test_disabled_binding_keeps_trigger_but_does_not_fire():
    calls: list[int] = []
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO processing",
            trigger="Ctrl+B",
            enabled=True,
        ),
        on_activate=lambda: calls.append(1),
    )

    registry.set_binding("micro.tune.toggle", enabled=False)
    binding = registry.require("micro.tune.toggle").binding
    assert binding.trigger == "Ctrl+B"
    assert binding.enabled is False
    assert registry.activate("micro.tune.toggle") is False
    assert calls == []


def test_conflicts_are_centralized_across_modules():
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="mixer.game.volume_up",
            description="GAME volume up",
            trigger="F16",
            module="mixer",
        )
    )
    registry.register(
        ShortcutBinding(
            shortcut_id="pads.pad_1",
            description="Play Pad 1",
            trigger="f16",
            module="pads",
        )
    )
    registry.register(
        ShortcutBinding(
            shortcut_id="voice_fx.toggle",
            description="Toggle Voice FX",
            trigger="F17",
            module="voice_fx",
        )
    )

    conflicts = registry.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].trigger == "F16"
    assert conflicts[0].shortcut_ids == (
        "mixer.game.volume_up",
        "pads.pad_1",
    )


def test_settings_and_local_editor_share_same_binding_object():
    registry = ShortcutRegistry()
    action = registry.register(
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO processing",
            trigger="Ctrl+B",
        )
    )

    # "Module editor"
    registry.set_binding("micro.tune.toggle", trigger="Ctrl+Shift+B")

    # "Settings -> Shortcuts" reads the same object.
    assert action.binding.trigger == "Ctrl+Shift+B"
    assert registry.require("micro.tune.toggle").binding is action.binding


def test_manager_is_backend_neutral():
    events: list[str] = []
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="mixer.chat.mute",
            description="Mute CHAT",
            trigger="F18",
        ),
        on_activate=lambda: events.append("mute"),
    )
    manager = ShortcutManager(registry)

    assert manager.activated("mixer.chat.mute") is True
    assert events == ["mute"]
    assert manager.deactivated("mixer.chat.mute") is True


def test_registry_saved_binding_overrides_default_without_replacing_action():
    calls: list[str] = []
    registry = ShortcutRegistry()
    action = registry.register(
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO Processing (TUNE)",
            trigger="Ctrl+B",
            enabled=True,
            module="micro_processing",
        ),
        on_activate=lambda: calls.append("toggle"),
    )
    registry.apply_saved_bindings([
        {
            "shortcut_id": "micro.tune.toggle",
            "trigger": "F23",
            "enabled": False,
        }
    ])
    assert registry.require("micro.tune.toggle") is action
    assert action.binding.trigger == "F23"
    assert action.binding.enabled is False
    assert registry.activate("micro.tune.toggle") is False
    assert calls == []


def test_kde_authority_blocks_app_activation_when_system_shortcut_is_disabled():
    calls: list[str] = []
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO processing",
            trigger="F23",
            enabled=True,
        ),
        on_activate=lambda: calls.append("toggle"),
    )
    manager = ShortcutManager(registry)
    manager.set_backend_state(
        "micro.tune.toggle",
        authoritative=True,
        registered=True,
        enabled=False,
        detail="KDE shortcut disabled",
    )

    assert manager.can_activate("micro.tune.toggle") is False
    assert manager.activated("micro.tune.toggle") is False
    assert calls == []


def test_kde_enabled_still_respects_secondary_app_switch():
    calls: list[str] = []
    registry = ShortcutRegistry()
    registry.register(
        ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO processing",
            trigger="F23",
            enabled=False,
        ),
        on_activate=lambda: calls.append("toggle"),
    )
    manager = ShortcutManager(registry)
    state = manager.set_backend_state(
        "micro.tune.toggle",
        authoritative=True,
        registered=True,
        enabled=True,
        trigger_description="F23",
    )

    assert state.enabled is True
    assert manager.activated("micro.tune.toggle") is False
    registry.set_binding("micro.tune.toggle", enabled=True)
    assert manager.activated("micro.tune.toggle") is True
    assert calls == ["toggle"]
