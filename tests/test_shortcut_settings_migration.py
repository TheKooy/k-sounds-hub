from __future__ import annotations

from ksound_hub.models import AppSettings


def _tune_binding(settings: AppSettings) -> dict:
    return next(
        item
        for item in settings.shortcut_bindings
        if item.get("shortcut_id") == "micro.tune.toggle"
    )


def test_legacy_tune_fields_migrate_to_central_binding():
    settings = AppSettings.from_dict({
        "micro_processing_ab_shortcut": "F23",
        "micro_processing_ab_shortcut_enabled": False,
    })
    binding = _tune_binding(settings)
    assert binding["trigger"] == "F23"
    assert binding["enabled"] is False


def test_existing_central_binding_wins_over_legacy_fields():
    settings = AppSettings.from_dict({
        "micro_processing_ab_shortcut": "Ctrl+B",
        "micro_processing_ab_shortcut_enabled": True,
        "shortcut_bindings": [
            {
                "shortcut_id": "micro.tune.toggle",
                "description": "Toggle MICRO Processing (TUNE)",
                "trigger": "F23",
                "enabled": False,
                "module": "micro_processing",
                "allow_hold": False,
            }
        ],
    })
    binding = _tune_binding(settings)
    assert binding["trigger"] == "F23"
    assert binding["enabled"] is False


def test_shortcut_bindings_are_written_to_settings_json_payload():
    settings = AppSettings.from_dict({
        "micro_processing_ab_shortcut": "F23",
        "micro_processing_ab_shortcut_enabled": False,
    })
    payload = settings.to_dict()
    assert payload["shortcut_bindings"] == settings.shortcut_bindings
