from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .dsp_registry import DspModuleRegistry
from .preset_engine import MAX_MODULES, PresetEngine, PresetValidationError


_STYLE = r"""
QDialog#presetLabDialog { background: rgba(8, 13, 19, 250); color: rgba(240, 248, 255, 245); }
QFrame#presetLabCard { background: rgba(255,255,255,9); border: 1px solid rgba(110,205,255,35); border-radius: 12px; }
QLabel#presetLabTitle { font-size: 18px; font-weight: 850; }
QLabel#presetLabHint { color: rgba(190,210,226,190); font-size: 10px; }
QLabel#presetLabModuleTitle { font-weight: 800; color: rgba(225,242,255,240); }
QLabel#presetLabStatus { color: rgba(130,220,255,220); font-size: 10px; }
QPushButton { min-height: 28px; padding: 0 10px; border-radius: 9px; background: rgba(255,255,255,12); border: 1px solid rgba(255,255,255,28); color: rgba(240,248,255,235); }
QPushButton:hover { background: rgba(70,145,190,50); border-color: rgba(110,205,255,75); }
QLineEdit, QComboBox, QDoubleSpinBox { min-height: 28px; border-radius: 8px; padding: 0 8px; background: rgba(0,0,0,120); border: 1px solid rgba(255,255,255,26); color: rgba(240,248,255,240); }
QScrollArea { border: none; background: transparent; }
"""


class ClickFocusedDoubleSpinBox(QDoubleSpinBox):
    """Let the window scroll unless the user explicitly clicked this value first."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class ClickFocusedSlider(QSlider):
    """Prevent accidental value edits while wheel-scrolling the Preset Lab."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class PresetLabDialog(QDialog):
    """Temporary functional editor for the generic DSP preset engine.

    The UI is intentionally utilitarian. It exists to validate the module/preset
    architecture and tune real DSP parameters before the final UX is designed.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        engine: PresetEngine,
        registry: DspModuleRegistry,
        state: dict,
        apply_callback: Callable[[dict], bool],
        library_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("presetLabDialog")
        self.setWindowTitle("K-Sounds Preset Lab")
        self.resize(720, 700)
        self.setMinimumSize(620, 520)
        self.setStyleSheet(_STYLE)

        self.engine = engine
        self.registry = registry
        self.apply_callback = apply_callback
        self.library_changed_callback = library_changed_callback
        self._syncing = False
        raw_preset = state.get("preset") if isinstance(state, dict) else None
        if not isinstance(raw_preset, dict):
            raw_preset = engine.new_preset("Custom Lab", "vfx")
        try:
            self.preset = engine.runtime_chain(raw_preset, "vfx")
        except Exception:
            self.preset = engine.new_preset("Custom Lab", "vfx")
        self.audition_enabled = bool(state.get("enabled", False)) if isinstance(state, dict) else False

        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(35)
        self._apply_timer.timeout.connect(self._flush_live_apply)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel("Preset Lab · live native DSP")
        title.setObjectName("presetLabTitle")
        root.addWidget(title)

        hint = QLabel(
            "Temporary engineering UI. Changes are hot-applied to the physical MICRO only. "
            "The .kspreset format is portable and already carries target/module compatibility metadata."
        )
        hint.setObjectName("presetLabHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        top = QFrame()
        top.setObjectName("presetLabCard")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(10, 9, 10, 9)
        top_layout.setSpacing(7)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(str(self.preset.get("name", "Custom Lab")))
        self.name_edit.textChanged.connect(self._on_name_changed)
        name_row.addWidget(self.name_edit, 1)
        target = QLabel("Target: Voice FX (live)")
        target.setObjectName("presetLabStatus")
        name_row.addWidget(target)
        top_layout.addLayout(name_row)

        audition_row = QHBoxLayout()
        self.audition_check = QCheckBox("Audition custom chain")
        self.audition_check.setChecked(self.audition_enabled)
        self.audition_check.toggled.connect(self._on_audition_changed)
        audition_row.addWidget(self.audition_check)
        audition_row.addStretch(1)
        self.wet_value = QLabel()
        self.wet_value.setObjectName("presetLabStatus")
        audition_row.addWidget(self.wet_value)
        top_layout.addLayout(audition_row)

        self.wet_slider = ClickFocusedSlider(Qt.Horizontal)
        self.wet_slider.setRange(0, 100)
        self.wet_slider.setValue(int(round(float(self.preset.get("wet_percent", 100.0)))))
        self.wet_slider.valueChanged.connect(self._on_wet_changed)
        top_layout.addWidget(self.wet_slider)
        self._update_wet_label()

        root.addWidget(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.module_host = QWidget()
        self.module_layout = QVBoxLayout(self.module_host)
        self.module_layout.setContentsMargins(0, 0, 0, 0)
        self.module_layout.setSpacing(8)
        scroll.setWidget(self.module_host)
        root.addWidget(scroll, 1)

        add_row = QHBoxLayout()
        self.add_combo = QComboBox()
        for spec in self.registry.for_target("vfx", runtime_only=True):
            self.add_combo.addItem(spec.name, spec.module_type)
        add_row.addWidget(self.add_combo, 1)
        add_button = QPushButton("+ Add module")
        add_button.clicked.connect(self._add_module)
        add_row.addWidget(add_button)
        root.addLayout(add_row)

        footer = QHBoxLayout()
        self.status_label = QLabel("Live chain ready")
        self.status_label.setObjectName("presetLabStatus")
        footer.addWidget(self.status_label, 1)
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        footer.addWidget(save)
        save_as = QPushButton("Save as…")
        save_as.clicked.connect(self._save_as)
        footer.addWidget(save_as)
        imp = QPushButton("Import…")
        imp.clicked.connect(self._import)
        footer.addWidget(imp)
        exp = QPushButton("Export…")
        exp.clicked.connect(self._export)
        footer.addWidget(exp)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)

        self._rebuild_modules()

    def sync_state(self, state: dict) -> None:
        if not isinstance(state, dict) or self._syncing:
            return
        preset = state.get("preset")
        if isinstance(preset, dict):
            try:
                self.preset = self.engine.runtime_chain(preset, "vfx")
            except Exception:
                pass
        self.audition_enabled = bool(state.get("enabled", self.audition_enabled))
        self._syncing = True
        try:
            self.name_edit.setText(str(self.preset.get("name", "Custom Lab")))
            self.audition_check.setChecked(self.audition_enabled)
            self.wet_slider.setValue(int(round(float(self.preset.get("wet_percent", 100.0)))))
            self._update_wet_label()
            self._rebuild_modules()
        finally:
            self._syncing = False

    def _rebuild_modules(self) -> None:
        while self.module_layout.count():
            item = self.module_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        modules = self.preset.get("modules", [])
        if not modules:
            empty = QLabel("No modules yet. Add one below; audition stays silent until the chain contains a module.")
            empty.setObjectName("presetLabHint")
            empty.setWordWrap(True)
            self.module_layout.addWidget(empty)
        for index, module in enumerate(modules):
            self.module_layout.addWidget(self._module_card(index, module))
        self.module_layout.addStretch(1)

    def _module_card(self, index: int, module: dict) -> QFrame:
        spec = self.registry.get(str(module.get("type", "")))
        card = QFrame()
        card.setObjectName("presetLabCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(spec.name if spec is not None else str(module.get("type", "Unknown")))
        title.setObjectName("presetLabModuleTitle")
        header.addWidget(title)
        header.addStretch(1)
        up = QPushButton("↑")
        up.setFixedWidth(34)
        up.setEnabled(index > 0)
        up.clicked.connect(lambda _=False, i=index: self._move_module(i, -1))
        header.addWidget(up)
        down = QPushButton("↓")
        down.setFixedWidth(34)
        down.setEnabled(index < len(self.preset.get("modules", [])) - 1)
        down.clicked.connect(lambda _=False, i=index: self._move_module(i, 1))
        header.addWidget(down)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda _=False, i=index: self._remove_module(i))
        header.addWidget(remove)
        layout.addLayout(header)

        if spec is None:
            return card
        description = QLabel(spec.description)
        description.setObjectName("presetLabHint")
        description.setWordWrap(True)
        layout.addWidget(description)
        params = module.setdefault("params", {})
        for param in spec.parameters:
            row = QHBoxLayout()
            row.addWidget(QLabel(param.label))
            editor = ClickFocusedDoubleSpinBox()
            editor.setRange(param.minimum, param.maximum)
            editor.setSingleStep(param.step)
            editor.setDecimals(2 if param.step < 1.0 else 0)
            editor.setSuffix(f" {param.unit}" if param.unit else "")
            editor.setValue(float(params.get(param.key, param.default)))
            editor.valueChanged.connect(
                lambda value, i=index, key=param.key: self._set_param(i, key, float(value))
            )
            row.addWidget(editor, 1)
            layout.addLayout(row)
        return card

    def _schedule_apply(self) -> None:
        if not self._syncing:
            self._apply_timer.start()

    def _flush_live_apply(self) -> None:
        try:
            normalized = self.engine.runtime_chain(self.preset, "vfx")
        except PresetValidationError as exc:
            self.status_label.setText(f"Invalid chain: {exc}")
            return
        self.preset = normalized
        ok = bool(self.apply_callback({"enabled": self.audition_enabled, "preset": normalized}))
        self.status_label.setText("Applied live" if ok else "Live apply failed")

    def _on_name_changed(self, text: str) -> None:
        if self._syncing:
            return
        self.preset["name"] = str(text or "Custom Lab").strip() or "Custom Lab"
        self._schedule_apply()

    def _on_audition_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        self.audition_enabled = bool(checked)
        self._schedule_apply()

    def _on_wet_changed(self, value: int) -> None:
        if self._syncing:
            return
        self.preset["wet_percent"] = float(value)
        self._update_wet_label()
        self._schedule_apply()

    def _update_wet_label(self) -> None:
        self.wet_value.setText(f"Chain wet {self.wet_slider.value()}%")

    def _set_param(self, index: int, key: str, value: float) -> None:
        if self._syncing:
            return
        modules = self.preset.get("modules", [])
        if 0 <= index < len(modules):
            modules[index].setdefault("params", {})[key] = value
            self._schedule_apply()

    def _move_module(self, index: int, delta: int) -> None:
        modules = self.preset.get("modules", [])
        target = index + delta
        if 0 <= index < len(modules) and 0 <= target < len(modules):
            modules[index], modules[target] = modules[target], modules[index]
            self._rebuild_modules()
            self._schedule_apply()

    def _remove_module(self, index: int) -> None:
        modules = self.preset.get("modules", [])
        if 0 <= index < len(modules):
            modules.pop(index)
            self._rebuild_modules()
            self._schedule_apply()

    def _add_module(self) -> None:
        module_type = str(self.add_combo.currentData() or "").strip().lower()
        spec = self.registry.get(module_type)
        if spec is None:
            return
        modules = self.preset.setdefault("modules", [])
        if len(modules) >= MAX_MODULES:
            self.status_label.setText(f"Chain limit: {MAX_MODULES} modules")
            return
        count = sum(1 for module in modules if str(module.get("type", "")) == module_type)
        if count >= spec.max_instances:
            self.status_label.setText(f"{spec.name}: maximum {spec.max_instances} instance(s)")
            return
        modules.append(
            {
                "id": f"{module_type}-{len(modules) + 1}",
                "type": module_type,
                "enabled": True,
                "params": {param.key: param.default for param in spec.parameters},
            }
        )
        self._rebuild_modules()
        self._schedule_apply()

    def _notify_library_changed(self) -> None:
        callback = self.library_changed_callback
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def _save(self) -> None:
        try:
            path = self.engine.save(self.preset)
        except Exception as exc:
            QMessageBox.warning(self, "Preset Lab", f"Could not save preset:\n{exc}")
            return
        self.status_label.setText(f"Saved · {path.name}")
        self._notify_library_changed()

    def _save_as(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Save preset as", "Preset name:", text=str(self.preset.get("name", "Custom Lab"))
        )
        if not ok or not str(name).strip():
            return
        copied = self.engine.clone(self.preset)
        copied["name"] = str(name).strip()
        copied["id"] = str(name).strip()
        try:
            normalized = self.engine.runtime_chain(copied, "vfx")
            path = self.engine.save(normalized)
        except Exception as exc:
            QMessageBox.warning(self, "Preset Lab", f"Could not save preset:\n{exc}")
            return
        self.preset = normalized
        self.name_edit.setText(str(self.preset.get("name", "Custom Lab")))
        self.status_label.setText(f"Saved · {path.name}")
        self._notify_library_changed()
        self._schedule_apply()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import K-Sounds preset", str(Path.home()), "K-Sounds Preset (*.kspreset)")
        if not path:
            return
        try:
            _, preset = self.engine.import_file(path)
            preset = self.engine.runtime_chain(preset, "vfx")
        except Exception as exc:
            QMessageBox.warning(self, "Preset Lab", f"Preset import rejected:\n{exc}")
            return
        self.preset = preset
        self.sync_state({"enabled": self.audition_enabled, "preset": preset})
        self.status_label.setText(f"Imported · {Path(path).name}")
        self._notify_library_changed()
        self._schedule_apply()

    def _export(self) -> None:
        default_name = f"{self.preset.get('id', 'preset')}.kspreset"
        path, _ = QFileDialog.getSaveFileName(self, "Export K-Sounds preset", str(Path.home() / default_name), "K-Sounds Preset (*.kspreset)")
        if not path:
            return
        try:
            result = self.engine.export_file(self.preset, path)
        except Exception as exc:
            QMessageBox.warning(self, "Preset Lab", f"Could not export preset:\n{exc}")
            return
        self.status_label.setText(f"Exported · {result.name}")
