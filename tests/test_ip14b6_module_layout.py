from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def test_ip14b6_build_marker_and_responsive_module_cards():
    assert 'APP_BUILD = "IP' in GLASS
    settings = GLASS.split("def _settings_page(self) -> QWidget:", 1)[1].split("def _module_state_text", 1)[0]
    assert "card_layout = QVBoxLayout(card)" in settings
    assert "top_row = QHBoxLayout()" in settings
    assert "actions_row = QHBoxLayout()" in settings
    assert "state_label.setWordWrap(True)" in settings
    assert "self.module_cards[module_id] = card" in settings
    assert "scroll.set_content_width_locked(True)" in settings
    assert "card_layout = QHBoxLayout(card)" not in settings


def test_module_actions_are_not_forced_into_the_header_row():
    settings = GLASS.split("def _settings_page(self) -> QWidget:", 1)[1].split("def _module_state_text", 1)[0]
    assert "top_row.addWidget(check" in settings
    assert "actions_row.addWidget(restart)" in settings
    assert "actions_row.addWidget(uninstall)" in settings
    assert settings.index("card_layout.addLayout(top_row)") < settings.index("card_layout.addLayout(actions_row)")
