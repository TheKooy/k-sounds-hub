from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


VOICE_FX_PRESETS = (
    ("distortion", "Distortion", "⚡", "Driven saturated voice for aggressive or comic effects."),
    ("pitch_down", "Pitch ↓", "▼", "Lower-pitched voice with a deliberately chunky character."),
    ("pitch_up", "Pitch ↑", "▲", "Higher-pitched voice for a bright cartoon-like character."),
    ("radio", "Radio Pilot", "✈", "Strong compressed aviation/intercom texture with comms grit."),
    ("telephone", "Telephone", "☎", "Old-handset narrowband voice with obvious 8 kHz line character."),
    ("robot", "Robot 1", "◉", "Validated IP13a ring-modulated robot voice."),
    ("reverb", "Reverb", "≈", "Compact fun room reverb that stacks cleanly with character effects."),
    ("church", "Church", "⌂", "Large natural church space with a plosive-safe reverb send."),
    ("church_pa", "Church PA", "▣", "Normal-level PA/micro voice with loudspeaker-coloured church reflections behind it."),
)
VOICE_FX_KEYS = {item[0] for item in VOICE_FX_PRESETS}
VOICE_FX_ORDER = tuple(item[0] for item in VOICE_FX_PRESETS)


_STYLE = r"""
QDialog#voiceFxDialog {
    background: rgba(8, 13, 19, 248);
    color: rgba(245, 249, 255, 245);
}
QDialog#voiceFxDialog[embedded="true"] {
    background: transparent;
}
QLabel#voiceFxTitle {
    font-size: 17px;
    font-weight: 850;
    color: rgba(248, 252, 255, 250);
}
QLabel#voiceFxHint,
QLabel#voiceFxPresetHint {
    color: rgba(195, 214, 230, 185);
    font-size: 10px;
}
QFrame#voiceFxAmountCard {
    background: rgba(255, 255, 255, 10);
    border: 1px solid rgba(110, 205, 255, 34);
    border-radius: 13px;
}
QPushButton#voiceFxPreset {
    min-height: 72px;
    text-align: left;
    padding: 8px 10px;
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 22);
    background: rgba(255, 255, 255, 8);
    color: rgba(225, 237, 247, 220);
    font-size: 11px;
    font-weight: 760;
}
QPushButton#voiceFxPreset:hover {
    background: rgba(60, 135, 180, 42);
    border: 1px solid rgba(110, 205, 255, 65);
}
QPushButton#voiceFxPreset:checked {
    background: rgba(38, 126, 170, 92);
    border: 1px solid rgba(105, 217, 255, 180);
    color: rgba(255, 255, 255, 250);
}
QSlider#voiceFxAmount::groove:horizontal {
    height: 6px;
    border-radius: 3px;
    background: rgba(0, 0, 0, 155);
}
QSlider#voiceFxAmount::sub-page:horizontal {
    border-radius: 3px;
    background: rgba(92, 204, 255, 170);
}
QSlider#voiceFxAmount::add-page:horizontal {
    border-radius: 3px;
    background: rgba(25, 41, 54, 160);
}
QSlider#voiceFxAmount::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -5px 0px;
    border-radius: 8px;
    background: rgba(120, 220, 255, 240);
}
QPushButton#voiceFxClose {
    min-height: 28px;
    padding: 0 16px;
    border-radius: 10px;
    background: rgba(255, 255, 255, 12);
    border: 1px solid rgba(255, 255, 255, 28);
    color: rgba(240, 248, 255, 235);
    font-weight: 760;
}
QPushButton#voiceFxClose:hover {
    background: rgba(70, 145, 190, 56);
    border: 1px solid rgba(110, 205, 255, 72);
}
QDialog#voiceFxDialog[embedded="true"] QLabel#voiceFxTitle {
    font-size: 15px;
}
QDialog#voiceFxDialog[embedded="true"] QLabel#voiceFxHint,
QDialog#voiceFxDialog[embedded="true"] QLabel#voiceFxPresetHint {
    font-size: 9px;
}
QDialog#voiceFxDialog[embedded="true"] QPushButton#voiceFxPreset {
    min-height: 46px;
    max-height: 52px;
    padding: 5px 8px;
    border-radius: 9px;
    font-size: 10px;
}
QDialog#voiceFxDialog[embedded="true"] QFrame#voiceFxAmountCard {
    border-radius: 10px;
}
"""


class VoiceFxDialog(QDialog):
    """Live multi-toggle editor for native MICRO Voice FX.

    There is deliberately no global enable switch: each effect card is its own
    toggle. Compatible effects stack in a fixed native DSP order. Radio and
    Telephone are mutually exclusive, Pitch ↑/↓ replace one another, and Reverb /
    Church / Church PA are mutually exclusive room choices.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        state: dict,
        apply_callback: Callable[[dict], bool],
        embedded: bool = False,
        custom_presets_provider: Callable[[], list[dict]] | None = None,
        custom_state: dict | None = None,
        custom_apply_callback: Callable[[dict], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._embedded = bool(embedded)
        self.setObjectName("voiceFxDialog")
        self.setProperty("embedded", "true" if self._embedded else "false")
        if self._embedded:
            self.setWindowFlags(Qt.Widget)
            self.setModal(False)
            self.setMinimumWidth(0)
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        else:
            self.setWindowTitle("MICRO Voice FX")
            self.setModal(True)
            self.resize(520, 390)
            self.setMinimumSize(470, 350)
        self.setStyleSheet(_STYLE)

        self._apply_callback = apply_callback
        self._custom_presets_provider = custom_presets_provider
        self._custom_apply_callback = custom_apply_callback
        self._custom_state = dict(custom_state or {})
        self._syncing = False
        requested = {
            str(item or "").strip().lower()
            for item in (state.get("active", []) if isinstance(state.get("active", []), list) else [])
        }
        self._active = [key for key in VOICE_FX_ORDER if key in requested]
        self._pending_amount = max(0, min(100, int(state.get("amount", 72))))
        self._amount_timer = QTimer(self)
        self._amount_timer.setSingleShot(True)
        self._amount_timer.setInterval(35)
        self._amount_timer.timeout.connect(self._flush_amount)

        root = QVBoxLayout(self)
        root.setContentsMargins(*( (2, 2, 2, 2) if self._embedded else (16, 15, 16, 14) ))
        root.setSpacing(6 if self._embedded else 11)

        if not self._embedded:
            title = QLabel("Voice FX · native low-latency")
            title.setObjectName("voiceFxTitle")
            root.addWidget(title)

            hint = QLabel(
                "Click an effect to toggle it. Compatible effects can stack. Radio/Telephone, "
                "Pitch ↑/↓ and the room effects replace their alternatives. Voice FX affect only the physical MICRO input; Soundboard and "
                "injected audio stay clean. Tune remains independent."
            )
            hint.setObjectName("voiceFxHint")
            hint.setWordWrap(True)
            root.addWidget(hint)

        preset_label = QLabel("Effects")
        preset_label.setObjectName("voiceFxPresetHint")
        if self._embedded:
            preset_label.setMinimumWidth(0)
            preset_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(preset_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(0 if self._embedded else 7)
        grid.setVerticalSpacing(5 if self._embedded else 7)
        self.preset_buttons: dict[str, QPushButton] = {}

        for index, (key, label, glyph, description) in enumerate(VOICE_FX_PRESETS):
            button = QPushButton(f"{glyph}  {label}\n{description}")
            button.setObjectName("voiceFxPreset")
            button.setCheckable(True)
            button.setProperty("presetKey", key)
            button.setToolTip(description)
            button.setChecked(key in self._active)
            button.clicked.connect(
                lambda checked=False, wanted=key: self._on_effect_clicked(wanted, bool(checked))
            )
            self.preset_buttons[key] = button
            if self._embedded:
                # Ignore the long text size hint: the button must follow the
                # drawer width rather than forcing its parent wider. Qt clips
                # the painted text inside the control; full copy remains in
                # the tooltip.
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                grid.addWidget(button, index, 0)
            else:
                grid.addWidget(button, index // 2, index % 2)

        root.addLayout(grid)

        self.custom_label = QLabel("Custom presets")
        self.custom_label.setObjectName("voiceFxPresetHint")
        self.custom_label.setMinimumWidth(0)
        self.custom_label.setVisible(False)
        root.addWidget(self.custom_label)

        self.custom_grid = QGridLayout()
        self.custom_grid.setHorizontalSpacing(0 if self._embedded else 7)
        self.custom_grid.setVerticalSpacing(5 if self._embedded else 7)
        self.custom_preset_buttons: dict[str, QPushButton] = {}
        self._custom_presets: dict[str, dict] = {}
        root.addLayout(self.custom_grid)
        self.refresh_custom_presets()

        amount_card = QFrame()
        amount_card.setObjectName("voiceFxAmountCard")
        if self._embedded:
            amount_card.setMinimumWidth(0)
            amount_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        amount_layout = QVBoxLayout(amount_card)
        amount_layout.setContentsMargins(*( (8, 5, 8, 6) if self._embedded else (12, 9, 12, 10) ))
        amount_layout.setSpacing(4 if self._embedded else 6)

        amount_header = QHBoxLayout()
        amount_title = QLabel("Strength / wet mix")
        amount_title.setObjectName("voiceFxPresetHint")
        self.amount_value = QLabel(f"{self._pending_amount}%")
        self.amount_value.setObjectName("voiceFxPresetHint")
        amount_header.addWidget(amount_title)
        amount_header.addStretch(1)
        amount_header.addWidget(self.amount_value)
        amount_layout.addLayout(amount_header)

        self.amount_slider = QSlider(Qt.Horizontal)
        self.amount_slider.setObjectName("voiceFxAmount")
        self.amount_slider.setRange(0, 100)
        self.amount_slider.setValue(self._pending_amount)
        self.amount_slider.valueChanged.connect(self._on_amount_changed)
        self.amount_slider.sliderReleased.connect(self._flush_amount)
        amount_layout.addWidget(self.amount_slider)
        root.addWidget(amount_card)

        if not self._embedded:
            footer = QHBoxLayout()
            footer.addStretch(1)
            close = QPushButton("Close")
            close.setObjectName("voiceFxClose")
            close.clicked.connect(self.accept)
            footer.addWidget(close)
            root.addLayout(footer)

    def _current_custom_id(self) -> str:
        state = self._custom_state if isinstance(self._custom_state, dict) else {}
        if not bool(state.get("enabled", False)):
            return ""
        preset = state.get("preset", {})
        if not isinstance(preset, dict):
            return ""
        return str(preset.get("id", "") or "").strip().lower()

    def refresh_custom_presets(self) -> None:
        while self.custom_grid.count():
            item = self.custom_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.custom_preset_buttons = {}
        self._custom_presets = {}

        provider = self._custom_presets_provider
        presets: list[dict] = []
        if provider is not None:
            try:
                raw = provider()
                if isinstance(raw, list):
                    presets = [item for item in raw if isinstance(item, dict)]
            except Exception:
                presets = []

        active_id = self._current_custom_id()
        for index, preset in enumerate(presets):
            preset_id = str(preset.get("id", "") or "").strip().lower()
            if not preset_id:
                continue
            name = str(preset.get("name", preset_id) or preset_id).strip()
            modules = preset.get("modules", [])
            module_count = len(modules) if isinstance(modules, list) else 0
            try:
                wet = max(0, min(100, int(round(float(preset.get("wet_percent", 100.0))))))
            except Exception:
                wet = 100
            description = f"Custom · {module_count} module{'s' if module_count != 1 else ''} · Wet {wet}%"
            button = QPushButton(f"✦  {name}\n{description}")
            button.setObjectName("voiceFxPreset")
            button.setCheckable(True)
            button.setProperty("customPresetId", preset_id)
            button.setToolTip(f"Saved .kspreset · {module_count} module(s)")
            button.setChecked(preset_id == active_id)
            button.clicked.connect(
                lambda checked=False, wanted=preset_id: self._on_custom_effect_clicked(wanted, bool(checked))
            )
            self.custom_preset_buttons[preset_id] = button
            self._custom_presets[preset_id] = preset
            if self._embedded:
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                self.custom_grid.addWidget(button, index, 0)
            else:
                self.custom_grid.addWidget(button, index // 2, index % 2)

        self.custom_label.setVisible(bool(self.custom_preset_buttons))

    def sync_custom_state(self, state: dict) -> None:
        self._custom_state = dict(state or {}) if isinstance(state, dict) else {}
        active_id = self._current_custom_id()
        self._syncing = True
        try:
            for preset_id, button in self.custom_preset_buttons.items():
                button.setChecked(preset_id == active_id)
        finally:
            self._syncing = False

    def _on_custom_effect_clicked(self, preset_id: str, enabled: bool) -> None:
        if self._syncing:
            return
        wanted = str(preset_id or "").strip().lower()
        preset = self._custom_presets.get(wanted)
        callback = self._custom_apply_callback
        if preset is None or callback is None:
            self.sync_custom_state(self._custom_state)
            return
        try:
            ok = bool(callback({"enabled": bool(enabled), "preset": preset}))
        except Exception:
            ok = False
        if not ok:
            self.sync_custom_state(self._custom_state)
            return
        self._custom_state = {"enabled": bool(enabled), "preset": preset}
        self.sync_custom_state(self._custom_state)

    def sync_state(self, state: dict) -> None:
        """Refresh the page from the central VFX state without re-applying it."""
        if not isinstance(state, dict):
            return
        requested = {
            str(item or "").strip().lower()
            for item in (state.get("active", []) if isinstance(state.get("active", []), list) else [])
        }
        self._active = [key for key in VOICE_FX_ORDER if key in requested]
        self._pending_amount = max(0, min(100, int(state.get("amount", self._pending_amount))))
        self._amount_timer.stop()
        self._sync_buttons()
        self.amount_slider.blockSignals(True)
        self.amount_slider.setValue(self._pending_amount)
        self.amount_slider.blockSignals(False)
        self.amount_value.setText(f"{self._pending_amount}%")

    def _apply(self, updates: dict) -> None:
        if self._syncing or not isinstance(updates, dict) or not updates:
            return
        try:
            self._apply_callback(dict(updates))
        except Exception:
            pass

    def _sync_buttons(self) -> None:
        self._syncing = True
        try:
            active = set(self._active)
            for key, button in self.preset_buttons.items():
                button.setChecked(key in active)
        finally:
            self._syncing = False

    def _on_effect_clicked(self, effect: str, enabled: bool) -> None:
        if self._syncing:
            return
        wanted = str(effect or "").strip().lower()
        if wanted not in VOICE_FX_KEYS:
            return

        active = [key for key in self._active if key in VOICE_FX_KEYS and key != wanted]
        if enabled:
            if wanted == "radio":
                active = [key for key in active if key != "telephone"]
            elif wanted == "telephone":
                active = [key for key in active if key != "radio"]
            elif wanted in {"pitch_down", "pitch_up"}:
                active = [key for key in active if key not in {"pitch_down", "pitch_up"}]
            elif wanted in {"reverb", "church", "church_pa"}:
                active = [key for key in active if key not in {"reverb", "church", "church_pa"}]
            active.append(wanted)

        active_set = set(active)
        self._active = [key for key in VOICE_FX_ORDER if key in active_set]
        self._sync_buttons()
        self._apply({"active": list(self._active)})

    def _on_amount_changed(self, value: int) -> None:
        self._pending_amount = max(0, min(100, int(value)))
        self.amount_value.setText(f"{self._pending_amount}%")
        self._amount_timer.start()

    def _flush_amount(self) -> None:
        self._amount_timer.stop()
        self._apply({"amount": int(self._pending_amount)})
