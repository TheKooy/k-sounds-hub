
from pathlib import Path

from ksound_hub.module_extensions import validate_extensions


def test_optional_settings_empty():
    assert validate_extensions({"id": "empty"}) == ()


def test_settings_and_desktop_share_components():
    source = (
        Path(__file__).resolve().parents[1]
        / "src/ksound_hub/glass_app.py"
    ).read_text()

    assert "self._external_surface_widget(surface)" in source
    assert 'extensions.active_surfaces("settings")' in source
    assert "self.module_ui_action_result.emit(" in source
