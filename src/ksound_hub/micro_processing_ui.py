from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSlider,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)


class ScrollPassthroughSlider(QSlider):
    """Slider that leaves mouse-wheel scrolling to the surrounding panel."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class VisibleCheckBox(QCheckBox):
    """Checkbox that keeps a real indicator square visible in both states."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxIndicator,
            option,
            self,
        )
        if not rect.isValid():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        border = QColor(118, 231, 220, 245) if self.isChecked() else QColor(190, 202, 220, 220)
        if not self.isEnabled():
            border.setAlpha(110)
        painter.setPen(QPen(border, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 2.5, 2.5)


class MicroProcessingDialog(QDialog):
    """UI-first control surface for the native MICRO processing chain."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        state: dict,
        apply_callback: Callable[[dict], bool],
        noise_suppression_available: bool = False,
        open_eq_callback: Callable[[], None] | None = None,
        activity_callback: Callable[[], dict] | None = None,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self._embedded = bool(embedded)
        self.setObjectName("microProcessingSurface")
        self.setProperty("embedded", "true" if self._embedded else "false")
        if self._embedded:
            self.setWindowFlags(Qt.Widget)
            self.setModal(False)
        else:
            self.setWindowTitle("MICRO Processing")
            self.setModal(True)
            self.resize(560, 720)
            self.setMinimumSize(500, 560)
        self._apply_callback = apply_callback
        self._state = dict(state or {})
        self._noise_available = bool(noise_suppression_available)
        self._open_eq_callback = open_eq_callback
        self._activity_callback = activity_callback

        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(90)
        self._apply_timer.timeout.connect(self._flush)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*( (2, 2, 2, 2) if self._embedded else (16, 14, 16, 14) ))
        outer.setSpacing(5 if self._embedded else 10)

        header = QHBoxLayout()
        title = QLabel("Tune · MICRO Processing" if self._embedded else "MICRO Processing")
        title.setObjectName("microProcessingTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.master_status = QLabel("")
        self.master_status.setObjectName("microProcessingStatus")
        header.addWidget(self.master_status)
        outer.addLayout(header)

        chain = QLabel("Input Gain  →  Noise Suppression  →  Gate  →  MICRO EQ  →  Compressor  →  De-esser  →  Limiter")
        chain.setWordWrap(True)
        chain.setObjectName("microProcessingHint")
        outer.addWidget(chain)

        master_box = QFrame()
        master_box.setObjectName("microProcessingMaster")
        master_layout = QVBoxLayout(master_box)
        master_layout.setContentsMargins(*( (8, 5, 8, 5) if self._embedded else (12, 10, 12, 10) ))
        master_layout.setSpacing(4 if self._embedded else 6)
        master_row = QHBoxLayout()
        self.master_toggle = VisibleCheckBox("Processing")
        self.master_toggle.setChecked(bool(self._state.get("processing_enabled", False)))
        self.master_toggle.setToolTip("Master bypass for the added MICRO DSP, including the smooth Gate. Hard Gate remains independent.")
        self.master_toggle.toggled.connect(self._master_changed)
        master_row.addWidget(self.master_toggle)
        master_row.addStretch(1)
        bypass = QPushButton("Bypass now")
        bypass.setToolTip("Immediately bypass Input Gain, Noise Suppression, Gate, EQ, Compressor, De-esser and Limiter.")
        bypass.clicked.connect(self._bypass_now)
        master_row.addWidget(bypass)
        self.ab_button = QPushButton("A/B Processing")
        self.ab_button.setToolTip("Instantly compare processed vs bypassed sound without changing any individual effect settings.")
        self.ab_button.clicked.connect(
            lambda: self.master_toggle.setChecked(not self.master_toggle.isChecked())
        )
        master_row.addWidget(self.ab_button)
        master_layout.addLayout(master_row)
        master_help = QLabel("Keep this on BYPASS while preparing Tune. The card-level Hard Gate is separate and still works normally.")
        master_help.setWordWrap(True)
        master_help.setObjectName("microProcessingHint")
        master_layout.addWidget(master_help)
        outer.addWidget(master_box)
        self._sync_master_status()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.verticalScrollBar().setSingleStep(34)
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(2, 2, 6, 2)
        root.setSpacing(5 if self._embedded else 10)

        gain_box = self._section(root, "1 · Input Gain", "READY", "Adjust level before every added processing stage. 0 dB is neutral.")
        self._gain_box = gain_box
        self.input_gain = self._slider_row(
            gain_box,
            "Gain",
            "Use this to feed the processing chain at a healthy level. It is bypassed when Processing is OFF.",
            -120,
            240,
            int(round(float(self._state.get("input_gain_db", 0.0)) * 10.0)),
            self._format_input_gain,
        )
        self.input_gain.valueChanged.connect(self._input_gain_changed)

        noise_status = "READY" if self._noise_available else "UNAVAILABLE"
        noise_box = self._section(root, "2 · Noise Suppression", noise_status, "Real RNNoise suppression. OFF or 0% is a true bypass; this locks itself if RNNoise is unavailable.")
        self.noise_toggle = VisibleCheckBox("Enable Noise Suppression")
        self.noise_toggle.setChecked(bool(self._state.get("noise_suppression_enabled", int(self._state.get("noise_suppression", 0)) > 0)))
        self.noise_toggle.setEnabled(self._noise_available)
        self.noise_toggle.toggled.connect(self._noise_toggle_changed)
        noise_box.addWidget(self.noise_toggle)
        self._noise_box = noise_box
        self.noise = self._slider_row(
            noise_box, "Strength",
            "Latency-aligned mix: 0% = dry reference, 100% = full RNNoise output.",
            0, 100, int(self._state.get("noise_suppression", 0)),
            lambda value: f"{value}%", enabled=self._noise_available,
        )
        self.noise.valueChanged.connect(self._noise_changed)
        backend = QLabel("RNNoise backend: READY" if self._noise_available else "RNNoise backend: unavailable — suppression stays bypassed.")
        backend.setWordWrap(True)
        backend.setObjectName("microProcessingReady" if self._noise_available else "microProcessingUnavailable")
        noise_box.addWidget(backend)

        gate_box = self._section(root, "3 · Gate", "READY · smooth", "Gentle downward gate for everyday mixing. Closed audio is reduced rather than hard-muted, with hold and a slow release to preserve word endings.")
        self.gate_toggle = VisibleCheckBox("Enable Gate")
        self.gate_toggle.setChecked(bool(self._state.get("gate_enabled", False)))
        self.gate_toggle.toggled.connect(self._gate_toggle_changed)
        gate_box.addWidget(self.gate_toggle)
        self._gate_box = gate_box
        self.gate_threshold = self._slider_row(
            gate_box,
            "Threshold",
            "Higher values close the soft Gate sooner. Hard Gate keeps its own independent threshold on the MICRO card.",
            0,
            100,
            int(self._state.get("gate_threshold", 35)),
            lambda value: f"{value}%",
        )
        self.gate_threshold.valueChanged.connect(self._gate_threshold_changed)

        eq_box = self._section(root, "4 · MICRO EQ", "READY", "Dedicated microphone EQ. Its profiles are separate from every playback/output EQ preset.")
        self._eq_box = eq_box
        eq_row = QHBoxLayout()
        eq_row.addWidget(QLabel("Selected profile"))
        eq_row.addStretch(1)
        self.eq_name = QLabel(str(self._state.get("eq_profile", "Default") or "Default"))
        self.eq_name.setObjectName("microProcessingValue")
        eq_row.addWidget(self.eq_name)
        eq_box.addLayout(eq_row)
        eq_note = QLabel("Open the dedicated MICRO EQ editor. These profiles never appear in the global output EQ page.")
        eq_note.setWordWrap(True)
        eq_note.setObjectName("microProcessingHint")
        eq_box.addWidget(eq_note)
        self.eq_edit_button = QPushButton("Edit MICRO EQ")
        self.eq_edit_button.setToolTip("Open the dedicated MICRO EQ editor.")
        self.eq_edit_button.setEnabled(self._open_eq_callback is not None)
        self.eq_edit_button.clicked.connect(self._open_eq_editor)
        eq_box.addWidget(self.eq_edit_button)

        comp_box = self._section(root, "5 · Compressor", "READY", "Reduces large voice-level jumps. Toggle and amount are independent so you can prepare the amount while bypassed.")
        self.compressor_toggle = VisibleCheckBox("Enable Compressor")
        self.compressor_toggle.setChecked(bool(self._state.get("compressor_enabled", False)))
        self.compressor_toggle.toggled.connect(self._compressor_toggle_changed)
        comp_box.addWidget(self.compressor_toggle)
        self._compressor_box = comp_box
        self.compressor = self._slider_row(
            comp_box,
            "Amount",
            "Higher values give stronger compression and more automatic makeup gain.",
            0,
            100,
            int(self._state.get("compressor_amount", 45)),
            lambda value: f"{value}%",
        )
        self.compressor.valueChanged.connect(self._compressor_changed)
        self.compressor_activity = QLabel("Reduction: —")
        self.compressor_activity.setObjectName("microProcessingActivity")
        self.compressor_activity.setToolTip("Live gain reduction reported by the native compressor.")
        comp_box.addWidget(self.compressor_activity)
        self.compressor_meter = self._make_reduction_meter(
            "Live compressor gain reduction · 0–24 dB scale"
        )
        comp_box.addWidget(self.compressor_meter)

        de_box = self._section(root, "6 · De-esser", "READY", "Softens harsh S / SH / CH sounds without lowering the whole microphone.")
        self.deesser_toggle = VisibleCheckBox("Enable De-esser")
        self.deesser_toggle.setChecked(bool(self._state.get("deesser_enabled", False)))
        self.deesser_toggle.toggled.connect(self._deesser_toggle_changed)
        de_box.addWidget(self.deesser_toggle)
        self._deesser_box = de_box
        self.deesser = self._slider_row(
            de_box,
            "Amount",
            "Higher values reduce the detected sibilance band more strongly.",
            0,
            100,
            int(self._state.get("deesser_amount", 35)),
            lambda value: f"{value}%",
        )
        self.deesser.valueChanged.connect(self._deesser_changed)
        self.deesser_activity = QLabel("Sibilance reduction: —")
        self.deesser_activity.setObjectName("microProcessingActivity")
        self.deesser_activity.setToolTip("Live high-band reduction reported by the native de-esser.")
        de_box.addWidget(self.deesser_activity)
        self.deesser_meter = self._make_reduction_meter(
            "Live de-esser reduction · 0–24 dB scale"
        )
        de_box.addWidget(self.deesser_meter)

        limiter_box = self._section(root, "7 · Limiter", "READY", "Final user-facing safety stage. The engine also keeps an emergency hard ceiling independently.")
        self.limiter_toggle = VisibleCheckBox("Enable Safety Limiter")
        self.limiter_toggle.setChecked(bool(self._state.get("limiter_enabled", True)))
        self.limiter_toggle.toggled.connect(self._limiter_changed)
        limiter_box.addWidget(self.limiter_toggle)
        self._limiter_box = limiter_box
        limiter_ceiling_db = max(-12.0, min(0.0, float(self._state.get("limiter_ceiling_db", -1.0))))
        self.limiter_ceiling = self._slider_row(
            limiter_box,
            "Ceiling",
            "Maximum level allowed by the user-facing limiter. Lower values clamp peaks earlier; -1 dB is the default.",
            -24,
            0,
            int(round(limiter_ceiling_db * 2.0)),
            lambda value: f"{value / 2.0:.1f} dB",
        )
        self.limiter_ceiling.valueChanged.connect(self._limiter_ceiling_changed)
        self.limiter_activity = QLabel("Peak reduction: —")
        self.limiter_activity.setObjectName("microProcessingActivity")
        self.limiter_activity.setToolTip("Live peak reduction reported by the native safety limiter.")
        limiter_box.addWidget(self.limiter_activity)
        self.limiter_meter = self._make_reduction_meter(
            "Live limiter gain reduction · 0–24 dB scale"
        )
        limiter_box.addWidget(self.limiter_meter)

        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(100)
        self._activity_timer.timeout.connect(self._poll_activity)
        if callable(self._activity_callback):
            self._activity_timer.start()
            self._poll_activity()

        self._sync_effect_card_visuals()

        root.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        reset = QPushButton("Reset processing")
        reset.setToolTip("Restore safe defaults and leave Processing in BYPASS.")
        reset.clicked.connect(self._reset)
        footer.addWidget(reset)
        footer.addStretch(1)
        if not self._embedded:
            close = QPushButton("Done")
            close.clicked.connect(self.accept)
            footer.addWidget(close)
        outer.addLayout(footer)

        self.setStyleSheet("""
QDialog { background: rgba(12, 16, 24, 248); color: rgba(240, 247, 255, 248); }
QDialog#microProcessingSurface[embedded="true"] { background: transparent; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QLabel#microProcessingTitle { font-size: 19px; font-weight: 900; }
QLabel#microProcessingHint { color: rgba(190, 210, 225, 235); font-size: 11px; }
QLabel#microProcessingUnavailable { color: rgba(255, 190, 105, 240); font-size: 11px; }
QLabel#microProcessingReady { color: rgba(120, 235, 170, 245); font-size: 11px; font-weight: 800; }
QLabel#microProcessingStatus { color: rgba(110, 220, 255, 250); font-size: 12px; font-weight: 900; }
QLabel#microProcessingSectionTitle { font-size: 13px; font-weight: 850; }
QLabel#microProcessingStageStatus { color: rgba(120, 220, 190, 245); font-size: 10px; font-weight: 850; }
QLabel#microProcessingValue { color: rgba(245,250,255,250); font-weight: 850; }
QLabel#microProcessingActivity { color: rgba(110,220,255,230); font-size: 11px; font-weight: 750; }
QProgressBar#microProcessingReductionMeter {
    min-height: 8px;
    max-height: 8px;
    border: 1px solid rgba(110,220,255,75);
    border-radius: 4px;
    background: rgba(255,255,255,18);
}
QProgressBar#microProcessingReductionMeter::chunk {
    border-radius: 3px;
    background: rgba(110,220,255,220);
}
QFrame#microProcessingMaster { background: rgba(70, 180, 230, 18); border: 1px solid rgba(100, 210, 255, 60); border-radius: 11px; }
QFrame#microProcessingSection { background: rgba(255,255,255,10); border: 1px solid rgba(255,255,255,24); border-radius: 11px; }
QCheckBox { font-size: 13px; font-weight: 750; spacing: 8px; }
QPushButton { min-height: 28px; padding: 2px 12px; border-radius: 8px; background: rgba(255,255,255,24); }
QPushButton:hover { background: rgba(255,255,255,38); }
QSlider::groove:horizontal { height: 5px; border-radius: 2px; background: rgba(255,255,255,42); }
QSlider::sub-page:horizontal { border-radius: 2px; background: rgba(76,205,255,170); }
QSlider::handle:horizontal { width: 15px; margin: -5px 0; border-radius: 7px; background: rgba(245,250,255,250); }
QScrollBar:vertical {
    background: rgba(255,255,255,8);
    width: 8px;
    margin: 2px 0 2px 0;
    border: none;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: rgba(170,215,235,120);
    min-height: 42px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover { background: rgba(190,230,248,180); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; border: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingTitle { font-size: 15px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingHint { font-size: 9px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingSectionTitle { font-size: 11px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingStatus { font-size: 10px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingActivity { font-size: 9px; }
QDialog#microProcessingSurface[embedded="true"] QCheckBox { font-size: 11px; spacing: 5px; }
QDialog#microProcessingSurface[embedded="true"] QPushButton { min-height: 21px; padding: 1px 7px; font-size: 10px; }
QDialog#microProcessingSurface[embedded="true"] QProgressBar#microProcessingReductionMeter { min-height: 6px; max-height: 6px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingStageStatus { font-size: 9px; }
QDialog#microProcessingSurface[embedded="true"] QLabel#microProcessingValue { font-size: 10px; }
QDialog#microProcessingSurface[embedded="true"] QSlider::groove:horizontal { height: 4px; }
QDialog#microProcessingSurface[embedded="true"] QSlider::handle:horizontal { width: 13px; margin: -4px 0; border-radius: 6px; }
""")

    def _section(self, parent: QVBoxLayout, title: str, status: str, help_text: str) -> QVBoxLayout:
        box = QFrame()
        box.setObjectName("microProcessingSection")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(*( (8, 5, 8, 5) if self._embedded else (12, 10, 12, 10) ))
        layout.setSpacing(4 if self._embedded else 7)
        row = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("microProcessingSectionTitle")
        row.addWidget(title_label)
        row.addStretch(1)
        status_label = QLabel(status)
        status_label.setObjectName("microProcessingStageStatus")
        row.addWidget(status_label)
        layout.addLayout(row)
        hint = QLabel(help_text)
        hint.setWordWrap(True)
        hint.setObjectName("microProcessingHint")
        layout.addWidget(hint)
        parent.addWidget(box)
        return layout

    def _slider_row(self, root: QVBoxLayout, name: str, tooltip: str, minimum: int, maximum: int, value: int, formatter, *, enabled: bool = True) -> QSlider:
        row = QHBoxLayout()
        label = QLabel(name)
        label.setToolTip(tooltip)
        row.addWidget(label)
        row.addStretch(1)
        value_label = QLabel("")
        value_label.setObjectName("microProcessingValue")
        value_label.setMinimumWidth(50 if self._embedded else 64)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(value_label)
        root.addLayout(row)
        slider = ScrollPassthroughSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(max(minimum, min(maximum, int(value))))
        slider.setEnabled(enabled)
        slider.setToolTip(tooltip)
        slider.valueChanged.connect(lambda current, target=value_label: target.setText(formatter(current)))
        slider._ksound_value_label = value_label
        slider._ksound_value_formatter = formatter
        value_label.setText(formatter(slider.value()))
        root.addWidget(slider)
        return slider

    def _open_eq_editor(self) -> None:
        callback = self._open_eq_callback
        if callback is None:
            return
        if not self._embedded:
            self.accept()
        QTimer.singleShot(0, callback)

    def sync_state(self, state: dict) -> None:
        """Refresh an embedded TUNE surface from the central MICRO state."""
        if not isinstance(state, dict):
            return
        self._apply_timer.stop()
        self._state = dict(state)

        def set_checked(widget, value: bool) -> None:
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)

        def set_value(widget, value: int) -> None:
            widget.blockSignals(True)
            widget.setValue(max(widget.minimum(), min(widget.maximum(), int(value))))
            widget.blockSignals(False)
            label = getattr(widget, "_ksound_value_label", None)
            formatter = getattr(widget, "_ksound_value_formatter", None)
            if label is not None and callable(formatter):
                label.setText(formatter(widget.value()))

        set_checked(self.master_toggle, self._state.get("processing_enabled", False))
        set_value(self.input_gain, round(float(self._state.get("input_gain_db", 0.0)) * 10.0))
        set_checked(self.noise_toggle, self._state.get("noise_suppression_enabled", False))
        set_value(self.noise, self._state.get("noise_suppression", 0))
        set_checked(self.gate_toggle, self._state.get("gate_enabled", False))
        set_value(self.gate_threshold, self._state.get("gate_threshold", 35))
        set_checked(self.compressor_toggle, self._state.get("compressor_enabled", False))
        set_value(self.compressor, self._state.get("compressor_amount", 45))
        set_checked(self.deesser_toggle, self._state.get("deesser_enabled", False))
        set_value(self.deesser, self._state.get("deesser_amount", 35))
        set_checked(self.limiter_toggle, self._state.get("limiter_enabled", True))
        limiter_db = max(-12.0, min(0.0, float(self._state.get("limiter_ceiling_db", -1.0))))
        set_value(self.limiter_ceiling, round(limiter_db * 2.0))
        if hasattr(self, "eq_name"):
            self.eq_name.setText(str(self._state.get("eq_profile", "Default") or "Default"))
        self._sync_master_status()
        self._sync_effect_card_visuals()

    @staticmethod
    def _format_input_gain(value: int) -> str:
        return f"{value / 10.0:+.1f} dB"

    def _set_effect_card_enabled(
        self,
        layout: QVBoxLayout,
        enabled: bool,
        *,
        available: bool = True,
        master_active: bool = True,
    ) -> None:
        # Visual bypass only: keep the checkbox and sliders interactive so a
        # value can be prepared while the DSP block is OFF.
        card = layout.parentWidget()
        if card is None:
            return

        effect = card.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(card)
            card.setGraphicsEffect(effect)

        if not available:
            effect.setOpacity(0.32)
        elif not master_active:
            effect.setOpacity(0.40)
        else:
            effect.setOpacity(1.0 if bool(enabled) else 0.46)

    def _sync_effect_card_visuals(self) -> None:
        master_active = bool(self.master_toggle.isChecked())
        self._set_effect_card_enabled(self._gain_box, True, master_active=master_active)
        self._set_effect_card_enabled(
            self._noise_box,
            bool(self.noise_toggle.isChecked()),
            available=bool(self._noise_available),
            master_active=master_active,
        )
        self._set_effect_card_enabled(self._gate_box, bool(self.gate_toggle.isChecked()), master_active=master_active)
        self._set_effect_card_enabled(self._eq_box, True, master_active=master_active)
        self._set_effect_card_enabled(
            self._compressor_box,
            bool(self.compressor_toggle.isChecked()),
            master_active=master_active,
        )
        self._set_effect_card_enabled(
            self._deesser_box,
            bool(self.deesser_toggle.isChecked()),
            master_active=master_active,
        )
        self._set_effect_card_enabled(
            self._limiter_box,
            bool(self.limiter_toggle.isChecked()),
            master_active=master_active,
        )

    def _make_reduction_meter(self, tooltip: str) -> QProgressBar:
        meter = QProgressBar(self)
        # Tenths of dB: 0..240 = 0.0..24.0 dB.
        meter.setRange(0, 240)
        meter.setValue(0)
        meter.setTextVisible(False)
        meter.setObjectName("microProcessingReductionMeter")
        meter.setToolTip(tooltip)
        meter.setAccessibleName(tooltip)
        meter.setFixedHeight(6 if self._embedded else 8)
        return meter

    def _poll_activity(self) -> None:
        callback = self._activity_callback
        if not callable(callback):
            return
        try:
            activity = callback()
        except Exception:
            activity = {}
        if not isinstance(activity, dict):
            activity = {}

        master = bool(self.master_toggle.isChecked())
        compressor_active = master and bool(self.compressor_toggle.isChecked())
        compressor_value = activity.get("compressor_reduction_db", 0.0)
        self._set_activity_text(
            self.compressor_activity,
            "Reduction",
            compressor_value,
            compressor_active,
        )
        self._set_activity_meter(
            self.compressor_meter,
            compressor_value,
            compressor_active,
        )
        deesser_active = master and bool(self.deesser_toggle.isChecked())
        deesser_value = activity.get("deesser_reduction_db", 0.0)
        self._set_activity_text(
            self.deesser_activity,
            "Sibilance reduction",
            deesser_value,
            deesser_active,
        )
        self._set_activity_meter(
            self.deesser_meter,
            deesser_value,
            deesser_active,
        )
        limiter_active = master and bool(self.limiter_toggle.isChecked())
        limiter_value = activity.get("limiter_reduction_db", 0.0)
        self._set_activity_text(
            self.limiter_activity,
            "Peak reduction",
            limiter_value,
            limiter_active,
        )
        self._set_activity_meter(
            self.limiter_meter,
            limiter_value,
            limiter_active,
        )

    @staticmethod
    def _set_activity_meter(meter: QProgressBar, value, active: bool) -> None:
        if not active:
            meter.setValue(0)
            meter.setEnabled(False)
            return
        try:
            reduction = max(0.0, min(24.0, float(value)))
        except Exception:
            reduction = 0.0
        meter.setEnabled(True)
        meter.setValue(int(round(reduction * 10.0)))

    @staticmethod
    def _set_activity_text(label: QLabel, prefix: str, value, active: bool) -> None:
        if not active:
            label.setText(f"{prefix}: —")
            return
        try:
            reduction = max(0.0, min(60.0, float(value)))
        except Exception:
            reduction = 0.0
        label.setText(f"{prefix}: {reduction:.1f} dB")

    def _sync_master_status(self) -> None:
        enabled = bool(self._state.get("processing_enabled", False))
        self.master_status.setText("ACTIVE" if enabled else "BYPASS")
        self.master_status.setToolTip("Added MICRO processing is active." if enabled else "Added MICRO processing is bypassed. Hard Gate remains independent.")

    def _master_changed(self, enabled: bool) -> None:
        self._state["processing_enabled"] = bool(enabled)
        self._sync_master_status()
        self._sync_effect_card_visuals()
        self._queue()

    def _bypass_now(self) -> None:
        self.master_toggle.setChecked(False)
        self._state["processing_enabled"] = False
        self._sync_master_status()
        self._flush()

    def _queue(self) -> None:
        self._apply_timer.start()

    def _flush(self) -> None:
        try:
            self._apply_callback(dict(self._state))
        except Exception:
            pass

    def _input_gain_changed(self, value: int) -> None:
        self._state["input_gain_db"] = value / 10.0
        self._queue()

    def _noise_toggle_changed(self, enabled: bool) -> None:
        if not self._noise_available:
            self._set_effect_card_enabled(self._noise_box, False, available=False)
            return
        self._set_effect_card_enabled(self._noise_box, bool(enabled))
        # ON/OFF is a pure bypass. Never erase the stored Strength value.
        # The only special case is a never-configured 0% value: first enable
        # starts at a useful 50%, then that value is remembered.
        if bool(enabled) and self.noise.value() <= 0:
            self.noise.setValue(50)
        self._state["noise_suppression_enabled"] = bool(enabled)
        self._queue()

    def _noise_changed(self, value: int) -> None:
        if self._noise_available:
            self._state["noise_suppression"] = max(0, min(100, int(value)))
            self._queue()

    def _gate_toggle_changed(self, enabled: bool) -> None:
        self._set_effect_card_enabled(self._gate_box, bool(enabled))
        self._state["gate_enabled"] = bool(enabled)
        self._queue()

    def _gate_threshold_changed(self, value: int) -> None:
        self._state["gate_threshold"] = max(0, min(100, int(value)))
        self._queue()

    def _compressor_toggle_changed(self, enabled: bool) -> None:
        self._set_effect_card_enabled(self._compressor_box, bool(enabled))
        self._state["compressor_enabled"] = bool(enabled)
        self._queue()

    def _compressor_changed(self, value: int) -> None:
        self._state["compressor_amount"] = max(0, min(100, int(value)))
        self._queue()

    def _deesser_toggle_changed(self, enabled: bool) -> None:
        self._set_effect_card_enabled(self._deesser_box, bool(enabled))
        self._state["deesser_enabled"] = bool(enabled)
        self._queue()

    def _deesser_changed(self, value: int) -> None:
        self._state["deesser_amount"] = max(0, min(100, int(value)))
        self._queue()

    def _limiter_changed(self, enabled: bool) -> None:
        self._set_effect_card_enabled(self._limiter_box, bool(enabled))
        self._state["limiter_enabled"] = bool(enabled)
        self._queue()

    def _limiter_ceiling_changed(self, value: int) -> None:
        self._state["limiter_ceiling_db"] = max(-12.0, min(0.0, float(value) / 2.0))
        self._queue()

    def _reset(self) -> None:
        self._state.update({
            "processing_enabled": False,
            "input_gain_db": 0.0,
            "noise_suppression_enabled": False,
            "noise_suppression": 50,
            "gate_enabled": False,
            "gate_threshold": 35,
            "compressor_enabled": False,
            "compressor_amount": 45,
            "deesser_enabled": False,
            "deesser_amount": 35,
            "limiter_enabled": True,
            "limiter_ceiling_db": -1.0,
        })
        self.master_toggle.blockSignals(True)
        self.master_toggle.setChecked(False)
        self.master_toggle.blockSignals(False)
        self.input_gain.setValue(0)
        if self._noise_available:
            self.noise_toggle.setChecked(False)
            self.noise.setValue(50)
        self.gate_toggle.setChecked(False)
        self.gate_threshold.setValue(35)
        self.compressor_toggle.setChecked(False)
        self.compressor.setValue(45)
        self.deesser_toggle.setChecked(False)
        self.deesser.setValue(35)
        self.limiter_toggle.setChecked(True)
        self.limiter_ceiling.setValue(-2)
        self._sync_master_status()
        self._sync_effect_card_visuals()
        self._flush()
