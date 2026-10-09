from __future__ import annotations
import os

"""Official K-Sounds Hub Glass frontend."""

import copy
import hashlib
import json
import re
import math
import secrets
import socket
import queue
import subprocess
import sys
import threading
import time
import numpy as np
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QEvent, QObject, QPoint, QProcess, QProcessEnvironment, QRect, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPixmap, QRegion, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGraphicsBlurEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSlider,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QDialog,
    QFileDialog,
    QWidgetAction,
    QSystemTrayIcon,
)


from .audio.pipewire_v2_final import PipeWireAudioEngine
from .app_routing import channel_for_media_role, process_display_name_from_cmdline, process_route_rule_from_cmdline, stream_display_name_from_data
from .config import CONFIG_DIR, IPC_SOCKET_PATH
from .control import resolve_ipc_socket_path
from .desktop_integration import autostart_enabled, set_autostart_enabled
from .ipc import AudioIpcServer
from .models import EqProfile
from .module_registry import ModuleRegistry
from .module_manager import ModuleManager, ModulePhase
from .module_packages import LocalModulePackageInstaller
from .module_host import ExternalModuleHost, runner_path
from .module_extensions import ExtensionRegistry, ExtensionSurface
from .module_remote_bridge import publish as publish_remote_surfaces, valid_value as remote_valid_value, FILE_NAME as REMOTE_SURFACES_FILE
from .module_remote_bridge import RECEIPT_DIR, receipt_finish
from .module_ui_state import checked_state
from .runtime_supervisor import RuntimeAdapter, RuntimeSupervisor
from .dsp_registry import DspModuleRegistry
from .preset_engine import PresetEngine, PresetValidationError
from .preset_lab_ui import PresetLabDialog
from .micro_processing_ui import MicroProcessingDialog, VisibleCheckBox
from .voice_fx_ui import VoiceFxDialog
from .settings_store import SettingsStore
from .shortcuts import ShortcutBinding, ShortcutManager, ShortcutRegistry, normalize_shortcut_trigger, shortcut_trigger_is_reserved
from .soundboard_service import SoundboardService
from .soundboard_remote_runtime import (
    RemoteRuntimeConfig,
    ensure_remote_server,
    remote_server_reachable,
    stop_remote_server,
)
from .ui.overlay import OverlayManager

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
HUD_LAUNCHER = PROJECT_ROOT / "scripts" / "start_hud.sh"
APP_ICON = PACKAGE_ROOT / "assets/app_icon.png"
APP_BG = PACKAGE_ROOT / "assets/backgrounds/ksound_hub_wallpaper_4k_blurfill_3840x2160.png"
CHANNEL_ICON_DIR = PACKAGE_ROOT / "assets/icons/channels"
CHANNEL_ICON_PATHS = {
    "all": CHANNEL_ICON_DIR / "all.png",
    "game": CHANNEL_ICON_DIR / "game.png",
    "chat": CHANNEL_ICON_DIR / "chat.png",
    "media": CHANNEL_ICON_DIR / "media.png",
    "more": CHANNEL_ICON_DIR / "more.png",
    "micro": CHANNEL_ICON_DIR / "micro.png",
    "return-mic": CHANNEL_ICON_DIR / "return-mic.png",
}
SOUNDBOARD_PATH = CONFIG_DIR / "soundboard.json"
APP_BUILD = "IP15m12"
SOUNDBOARD_RESTART_PRE_MUTE_SECONDS = 0.03
SOUNDBOARD_RESTART_POST_MUTE_SECONDS = 0.12

def soundboard_pad_shortcut_id(slot_key: str) -> str:
    stable = str(slot_key or "").strip()
    digest = hashlib.sha256(stable.encode("utf-8", "ignore")).hexdigest()[:20]
    return f"soundboard.pad.{digest}"


def micro_eq_preset_shortcut_id(profile_name: str) -> str:
    stable = str(profile_name or "").strip()
    digest = hashlib.sha256(stable.encode("utf-8", "ignore")).hexdigest()[:20]
    return f"micro.eq_preset.{digest}"


SOUNDBOARD_AUTO_PALETTES = (
    ((24, 72, 126), (90, 45, 142)),
    ((18, 96, 105), (30, 68, 142)),
    ((115, 47, 86), (48, 65, 145)),
    ((122, 66, 28), (125, 35, 79)),
    ((37, 101, 70), (33, 65, 130)),
    ((73, 55, 132), (22, 99, 116)),
    ((126, 43, 49), (70, 48, 137)),
    ((37, 73, 143), (112, 41, 115)),
)


def _soundboard_auto_palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Choose a stable colourful fallback without storing or generating an image."""
    digest = hashlib.sha256(str(seed or "sound").encode("utf-8", "ignore")).digest()
    return SOUNDBOARD_AUTO_PALETTES[int.from_bytes(digest[:2], "big") % len(SOUNDBOARD_AUTO_PALETTES)]


def _eq_snapshot_effective_profile(snapshot: dict, channel_key: str):
    """Return only the profile data that can change the audio graph."""
    if not isinstance(snapshot, dict):
        return None
    profiles = snapshot.get("profiles")
    selected = snapshot.get("selected")
    if not isinstance(profiles, list) or not profiles:
        return None
    selected_name = str(selected.get(channel_key) or "") if isinstance(selected, dict) else ""
    profile = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and str(item.get("name") or "") == selected_name
        ),
        None,
    )
    if profile is None:
        profile = next((item for item in profiles if isinstance(item, dict)), None)
    if profile is None:
        return None
    # A rename or a selection between two identical presets changes the UI and
    # saved state, but not PipeWire. Excluding the name avoids a needless
    # filter-chain hot-swap in those cases.
    return {key: value for key, value in profile.items() if key != "name"}


def _eq_snapshot_changed_channels(before: dict, after: dict, channel_keys) -> list[str]:
    """Find playback channels whose effective EQ really changed."""
    changed = []
    for raw_key in channel_keys:
        key = str(raw_key or "").strip()
        if not key:
            continue
        if _eq_snapshot_effective_profile(before, key) != _eq_snapshot_effective_profile(after, key):
            changed.append(key)
    return changed

def _qss_url(path: str) -> str:
    return str(path or "").replace("\\", "/").replace('"', '\\"')

SETTINGS_PATH = CONFIG_DIR / "settings.json"


def _read_window_settings_document() -> dict:
    try:
        if SETTINGS_PATH.is_file():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_window_settings_document(data: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def _window_geometry_rect(window) -> QRect:
    try:
        normal = window.normalGeometry()
        if normal.isValid() and normal.width() > 0 and normal.height() > 0:
            return normal
    except Exception:
        pass
    return window.geometry()


def _save_window_geometry(window, key: str) -> None:
    # Size-only on KDE Wayland: KWin owns window placement, but app-side size
    # memory is reliable and avoids fighting the compositor.
    if not key:
        return

    try:
        size = window.size()
        width = int(size.width())
        height = int(size.height())
        if width < 160 or height < 120:
            rect = _window_geometry_rect(window)
            width = int(rect.width())
            height = int(rect.height())

        if width < 160 or height < 120:
            return

        data = _read_window_settings_document()
        previous = data.get(key)
        if not isinstance(previous, dict):
            previous = {}

        data[key] = {
            "w": width,
            "h": height,
            "maximized": bool(window.isMaximized()),
        }
        _write_window_settings_document(data)
    except Exception:
        pass


def _queue_window_geometry_save(window, key: str, timer: QTimer | None) -> None:
    try:
        if timer is None or not window.isVisible():
            return
        timer.start()
    except Exception:
        pass


def _restore_window_geometry(window, key: str, default_w: int, default_h: int, min_w: int, min_h: int) -> None:
    data = _read_window_settings_document()
    geometry = data.get(key)
    if not isinstance(geometry, dict):
        window.resize(default_w, default_h)
        return

    try:
        width = max(min_w, min(5000, int(geometry.get("w", default_w))))
        height = max(min_h, min(3000, int(geometry.get("h", default_h))))
    except Exception:
        window.resize(default_w, default_h)
        return

    def apply_size() -> None:
        try:
            window.resize(width, height)
            if bool(geometry.get("maximized", False)):
                window.showMaximized()
        except Exception:
            pass

    apply_size()
    QTimer.singleShot(0, apply_size)
    QTimer.singleShot(220, apply_size)
    QTimer.singleShot(900, apply_size)
    QTimer.singleShot(1600, apply_size)


def _background_factor_from_slider(value: int) -> float:
    try:
        numeric = max(0, min(100, int(value)))
    except Exception:
        numeric = 50
    return max(0.0, min(2.0, numeric / 50.0))


def _background_source_for_settings(settings) -> QPixmap:
    if bool(getattr(settings, "wallpaper_enabled", False)):
        path = Path(str(getattr(settings, "wallpaper_path", "") or "")).expanduser()
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return pixmap
    return QPixmap(str(APP_BG)) if APP_BG.is_file() else QPixmap()


def _adjust_background_pixmap(pixmap: QPixmap, saturation: float, contrast: float) -> QPixmap:
    # Vectorized color transform: do not iterate ~921k pixels in Python.
    if pixmap.isNull():
        return pixmap

    try:
        saturation = max(0.0, min(2.0, float(saturation)))
    except Exception:
        saturation = 1.0

    try:
        contrast = max(0.0, min(2.0, float(contrast)))
    except Exception:
        contrast = 1.0

    if abs(saturation - 1.0) < 0.01 and abs(contrast - 1.0) < 0.01:
        return pixmap

    try:
        fmt = getattr(QImage, "Format_ARGB32", None)
        if fmt is None:
            fmt = QImage.Format.Format_ARGB32

        image = pixmap.toImage().convertToFormat(fmt)
        width = int(image.width())
        height = int(image.height())
        bytes_per_line = int(image.bytesPerLine())
        if width <= 0 or height <= 0 or bytes_per_line < width * 4:
            return pixmap

        raw = np.frombuffer(
            image.bits(),
            dtype=np.uint8,
            count=int(image.sizeInBytes()),
        )
        rows = raw.reshape((height, bytes_per_line))
        pixels = rows[:, : width * 4].reshape((height, width, 4))

        # QImage.Format_ARGB32 is B, G, R, A in memory on little-endian Linux.
        bgr = pixels[:, :, :3].astype(np.float32, copy=True)
        blue = bgr[:, :, 0]
        green = bgr[:, :, 1]
        red = bgr[:, :, 2]
        gray = (0.299 * red) + (0.587 * green) + (0.114 * blue)

        blue = gray + (blue - gray) * saturation
        green = gray + (green - gray) * saturation
        red = gray + (red - gray) * saturation

        blue = 128.0 + (blue - 128.0) * contrast
        green = 128.0 + (green - 128.0) * contrast
        red = 128.0 + (red - 128.0) * contrast

        pixels[:, :, 0] = np.clip(blue, 0.0, 255.0).astype(np.uint8)
        pixels[:, :, 1] = np.clip(green, 0.0, 255.0).astype(np.uint8)
        pixels[:, :, 2] = np.clip(red, 0.0, 255.0).astype(np.uint8)
        return QPixmap.fromImage(image)
    except Exception:
        return pixmap

def _send_ksh_ipc_payload(payload: dict) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(resolve_ipc_socket_path())
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        return True
    except Exception:
        return False


def _read_saved_mixer_channel_state() -> dict[str, tuple[int, bool]]:
    if not SETTINGS_PATH.is_file():
        return {}

    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, list):
        return {}

    state: dict[str, tuple[int, bool]] = {}
    for channel in channels:
        if not isinstance(channel, dict):
            continue

        key = str(channel.get("key") or "").strip()
        if not key:
            continue

        try:
            volume = max(0, min(100, int(channel.get("volume", 0))))
        except Exception:
            volume = 0

        state[key] = (volume, bool(channel.get("muted", False)))

    return state


GLASS_CHANNEL_KEY_ALIASES = {
    "all": "all",
    "game": "game",
    "chat": "chat",
    "media": "media",
    "more": "more",
    "micro": "micro",
    "retour": "return-mic",
    "retourmic": "return-mic",
    "return-mic": "return-mic",
    "return_mic": "return-mic",
}

GLASS_CHANNEL_OVERLAY_META = {
    "all": ("🌍", "ALL"),
    "game": ("🎮", "GAME"),
    "chat": ("💬", "CHAT"),
    "media": ("🎵", "MEDIA"),
    "more": ("🔊", "MORE"),
    "micro": ("🎤", "MICRO"),
    "return-mic": ("🎧", "MIC OUT"),
}

class GlassBackendController(QObject):
    channel_state_changed = Signal(str, int, bool)
    overlay_message_requested = Signal(str, bool)
    status_changed = Signal(str)
    restore_requested = Signal()
    remote_status_changed = Signal(str)
    soundboard_changed = Signal()
    soundboard_shortcut_edit_requested = Signal(str, object, object)
    soundboard_shortcut_state_changed = Signal(str, str, bool)
    micro_processing_state_changed = Signal()
    micro_voice_fx_state_changed = Signal(bool)
    micro_custom_fx_state_changed = Signal()
    module_state_changed = Signal(str)
    module_ui_state_changed = Signal(str)
    module_ui_action_result = Signal(str, str, str, bool, str)
    _module_transition_result = Signal(str, bool, bool, str)
    _module_restart_result = Signal(str, bool, str)
    _runtime_reconcile_result = Signal(bool, str)
    _remote_watchdog_result = Signal(str, str)
    _realtime_volume_error = Signal(str)
    ui_restart_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preserve_audio_attach = str(os.environ.get("KSH_PRESERVE_AUDIO_ROUTING", "0")).strip().lower() in {"1", "true", "yes", "on"}
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        self.module_registry = ModuleRegistry(self.settings)
        self.module_manager = ModuleManager(self.module_registry)
        self.module_package_installer = LocalModulePackageInstaller(self.module_manager)
        self.external_module_host = ExternalModuleHost(self.module_manager)
        self.extension_registry = ExtensionRegistry(self.module_registry, self.external_module_host)
        self._module_remote_path = CONFIG_DIR / REMOTE_SURFACES_FILE

        self._module_values_lock = threading.RLock()
        self._module_values: dict[str, dict] = {}

        self.module_state_changed.connect(
            self._schedule_module_ui_snapshot
        )
        self.module_state_changed.connect(
            lambda _mid: self._publish_module_remote_surfaces()
        )
        self.dsp_module_registry = DspModuleRegistry.default()
        self.preset_engine = PresetEngine(self.dsp_module_registry)
        self._link_shared_eq_library()
        self.audio_engine = PipeWireAudioEngine()
        self._metering_enabled = bool(getattr(self.settings, "visualizer_enabled", True))
        self.soundboard_service = SoundboardService()
        self.runtime_supervisor = RuntimeSupervisor(
            self.module_manager,
            self.module_registry,
        )
        self._register_runtime_units()
        self._module_transition_result.connect(self._finish_module_transition)
        self._module_restart_result.connect(self._finish_module_runtime_restart)
        self._runtime_reconcile_result.connect(self._finish_runtime_reconcile)
        # Session-local proof that a requested restart actually completed.
        # The counter only increments after the Runtime Supervisor reports
        # stop + start + health success; accepting the button click is not enough.
        self._module_restart_counts: dict[str, int] = {}

        self._micro_gate_stop = threading.Event()
        self._micro_gate_thread: threading.Thread | None = None
        self._micro_gate_process: subprocess.Popen | None = None
        self._micro_gate_source_name: str = ""
        self._micro_gate_open: bool = True
        self._micro_gate_last_above: float = 0.0
        self._micro_gate_last_apply: float = 0.0

        self._pending_apply_keys: set[str] = set()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(180)
        self._save_timer.timeout.connect(self._autosave)

        # Realtime volume applies MUST NOT run on the Qt GUI thread. Shortcut
        # holds and live slider drags only enqueue the channel key here; a
        # dedicated latest-value-wins worker performs state-file / PipeWire
        # work in the background. The worker reads the current in-memory
        # channel volume, so stale intermediate values are naturally skipped.
        self._realtime_volume_condition = threading.Condition()
        self._realtime_volume_pending: set[str] = set()
        self._realtime_volume_final_pending: set[str] = set()
        self._realtime_volume_stopping = False
        self._realtime_volume_error.connect(self.status_changed)
        self._realtime_volume_thread = threading.Thread(
            target=self._realtime_volume_worker,
            name="ksounds-volume-apply",
            daemon=True,
        )
        self._realtime_volume_thread.start()

        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(35)
        self._apply_timer.timeout.connect(self._flush_pending_channel_apply)

        # Undo/Redo restores the model synchronously but lets Qt paint the new
        # controls before any PipeWire hot-swap. Only one actually changed
        # channel is rebuilt per event-loop turn, keeping the UI responsive.
        self._pending_eq_history_apply_keys: list[str] = []
        self._eq_history_apply_timer = QTimer(self)
        self._eq_history_apply_timer.setSingleShot(True)
        self._eq_history_apply_timer.setInterval(0)
        self._eq_history_apply_timer.timeout.connect(self._flush_next_eq_history_apply)

        # Remote health is checked off the GUI thread. If the detached web
        # process dies while Glass stays alive, the same runtime manager that
        # owns normal startup is asked to recover it.
        self._remote_watchdog_shutdown = False
        self._remote_watchdog_inflight = False
        self._remote_watchdog_enabled = (
            os.environ.get("KSH_START_SOUNDBOARD_REMOTE", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        self._remote_watchdog_timer = QTimer(self)
        self._remote_watchdog_timer.setInterval(4000)
        self._remote_watchdog_timer.timeout.connect(self._queue_remote_watchdog_check)
        self._remote_watchdog_result.connect(self._apply_remote_watchdog_result)
        self._external_watchdog_timer = QTimer(self)
        self._external_watchdog_timer.setInterval(2000)
        self._external_watchdog_timer.timeout.connect(self._check_external_module_processes)
        self._external_watchdog_timer.timeout.connect(
            self._publish_module_remote_surfaces
        )
        self._external_watchdog_timer.start()

        # Physical MICRO presence guard. 500 ms gives quick fail-closed behavior
        # while keeping source enumeration out of the audio/DSP thread and out
        # of the high-frequency meter timer.
        self._micro_source_watchdog_timer = QTimer(self)
        self._micro_source_watchdog_timer.setInterval(500)
        self._micro_source_watchdog_timer.timeout.connect(
            self._reconcile_micro_source_presence
        )
        self._micro_source_watchdog_timer.start()
        if self._remote_watchdog_enabled:
            # Do one initial reconcile, but do not keep a 4 s timer alive when
            # Remote has no enabled Pads/VFX surface to serve.
            QTimer.singleShot(900, lambda: self._queue_remote_watchdog_check(force=True))

        self.ipc_server = AudioIpcServer(IPC_SOCKET_PATH, self)
        self.ipc_server.message_received.connect(self.handle_ipc_message)
        self.ipc_server.status_changed.connect(self.status_changed)
        self.ipc_server.start()

        # Apply backend after the UI has had a chance to appear.
        QTimer.singleShot(0, self.apply_startup_settings)
        QTimer.singleShot(1200, self._queue_runtime_reconcile_all)
        QTimer.singleShot(1500, self._restore_external_modules)
        QTimer.singleShot(
            1700,
            self._publish_module_remote_surfaces,
        )

    def _reconcile_micro_source_presence(self) -> None:
        engine = getattr(self, "audio_engine", None)
        reconcile = getattr(
            engine,
            "reconcile_selected_micro_source_presence",
            None,
        )
        if not callable(reconcile):
            return

        try:
            reconcile(self._audio_runtime_settings())
        except Exception:
            # A transient source-enumeration failure must not destabilize Glass.
            return

    def external_module_supported(self, module_id: str) -> bool:
        manifest = self.module_registry.manifest(module_id)
        if manifest is None or manifest.builtin or not self.module_registry.is_installed(module_id):
            return False
        try:
            runner_path(self.module_registry.external_root / module_id)
        except (ValueError, OSError, KeyError, TypeError):
            return False
        return True

    def _restore_external_modules(self) -> None:
        for manifest in self.module_registry.manifests():
            mid = manifest.module_id
            if manifest.builtin or not self.module_registry.is_enabled(mid):
                continue
            if not self.module_manager.begin(mid, ModulePhase.STARTING):
                continue
            self.module_state_changed.emit(mid)
            threading.Thread(
                target=self._module_transition_worker,
                args=(mid, True, True),
                name=f"ksh-external-restore-{mid}",
                daemon=True,
            ).start()

    def _publish_module_remote_surfaces(self) -> None:
        try:
            publish_remote_surfaces(
                self._module_remote_path,
                self.extension_registry.active_surfaces(
                    "remote"
                ),
                self._copy_confirmed_module_values(),
            )
        except Exception as exc:
            self.status_changed.emit(
                f"Module Remote publication failed: {exc}"
            )

    def _copy_confirmed_module_values(self) -> dict:
        with self._module_values_lock:
            return {
                mid: {
                    sid: dict(controls)
                    for sid, controls in states.items()
                }
                for mid, states in self._module_values.items()
            }

    def _accept_module_ui_state(
        self,
        module_id: str,
        candidate,
    ) -> bool:

        if not self.external_module_host.running(module_id):
            return False

        surfaces = tuple(
            surface
            for kind in ("desktop", "settings", "remote")
            for surface in self.extension_registry.active_surfaces(
                kind
            )
            if surface.module_id == module_id
        )

        validated = checked_state(
            surfaces,
            candidate,
        )

        if validated is None:
            self.status_changed.emit(
                f"Module {module_id}: invalid UI state ignored"
            )
            return False

        with self._module_values_lock:

            previous = self._module_values.get(
                module_id,
                {},
            )

            merged = {
                sid: dict(controls)
                for sid, controls in previous.items()
            }

            for sid, controls in validated.items():
                merged.setdefault(
                    sid,
                    {},
                ).update(controls)

            self._module_values[module_id] = merged

        self.module_ui_state_changed.emit(module_id)
        self._publish_module_remote_surfaces()

        return True

    def _schedule_module_ui_snapshot(
        self,
        module_id: str,
    ) -> None:

        if not self.external_module_host.running(module_id):

            with self._module_values_lock:
                self._module_values.pop(
                    module_id,
                    None,
                )

            return

        def fetch():

            try:
                reply = self.external_module_host.request(
                    module_id,
                    "ui.snapshot",
                    {},
                    timeout=2.0,
                )

                result = reply.get(
                    "result",
                    {},
                )

                if (
                    reply.get("ok")
                    and isinstance(result, dict)
                    and "state" in result
                ):
                    self._accept_module_ui_state(
                        module_id,
                        result["state"],
                    )

            except Exception:
                # Optional for older modules.
                pass

        threading.Thread(
            target=fetch,
            name=f"ksh-ui-state-{module_id}",
            daemon=True,
        ).start()

    def _check_external_module_processes(self) -> None:
        for mid in self.external_module_host.crashed():
            # Runtime failure is runtime truth, not a user preference change.
            self.module_manager.mark_runtime_failure(
                mid,
                "External module process exited unexpectedly",
            )

            # Confirmed UI values belonged to the dead worker instance.
            with self._module_values_lock:
                self._module_values.pop(mid, None)

            self.module_ui_state_changed.emit(mid)
            self.status_changed.emit(
                f"Module {mid}: process exited unexpectedly · restart available"
            )

            # Active extension surfaces are live-runtime gated.
            self.module_state_changed.emit(mid)

    def module_enabled(self, module_id: str) -> bool:
        return bool(self.module_registry.is_enabled(module_id))

    def module_state_snapshot(self) -> dict:
        snapshot = self.module_manager.snapshot()
        snapshot.update(self.runtime_supervisor.snapshot())
        return snapshot

    def module_status(self, module_id: str):
        return self.module_manager.status(module_id)

    def invoke_module_ui_action(self, module_id: str, surface_id: str,
                                control_id: str, action: str, value, request_id: str | None = None) -> bool:
        """Never wait on a subprocess from the Qt event thread."""
        registry = self.extension_registry

        def report(ok, detail):
            if request_id is not None:
                try:
                    receipt_finish(
                        CONFIG_DIR / RECEIPT_DIR,
                        request_id,
                        ok,
                        detail,
                    )
                except OSError as exc:
                    self.status_changed.emit(
                        f"Remote receipt error: {exc}"
                    )
        if not registry.action_allowed(module_id, surface_id, control_id, action):
            report(False, "Module or control unavailable")
            self.status_changed.emit(f"Module {module_id}: UI action rejected")
            return False

        def worker() -> None:
            if not registry.action_allowed(module_id, surface_id, control_id, action):
                report(False, "Module stopped before action")
                return
            try:
                result = self.external_module_host.request(module_id, "ui.action", {
                    "surface": surface_id, "control": control_id,
                    "action": action, "value": value,
                })
                if not result.get("ok"):
                    detail = str(result.get("error", "action refused"))[:180]
                    self.status_changed.emit(f"Module {module_id}: action refused — {detail}")
                    report(False, detail)
                    self.module_ui_action_result.emit(module_id, surface_id, control_id, False, detail)
                else:
                    self.status_changed.emit(f"Module {module_id}: action {action} accepted")
                    confirmed = result.get("result")

                    if (
                        isinstance(confirmed, dict)
                        and "state" in confirmed
                    ):
                        self._accept_module_ui_state(
                            module_id,
                            confirmed["state"],
                        )

                    report(True, "Accepted by module")
                    self.module_ui_action_result.emit(module_id, surface_id, control_id, True, "Accepted")
            except Exception as exc:
                detail = str(exc)[:180]
                self.status_changed.emit(f"Module {module_id}: UI action error — {detail}")
                report(False, detail)
                self.module_ui_action_result.emit(module_id, surface_id, control_id, False, detail)

        threading.Thread(target=worker, name=f"ksh-ui-{module_id}", daemon=True).start()
        return True

    def module_restart_count(self, module_id: str) -> int:
        wanted = str(module_id or "").strip().lower()
        return int(self._module_restart_counts.get(wanted, 0))

    def _remote_runtime_config(self) -> RemoteRuntimeConfig:
        return RemoteRuntimeConfig.from_environment(PROJECT_ROOT).with_surfaces(
            self.module_registry.remote_surfaces()
        )

    def _register_runtime_units(self) -> None:
        """Register concrete runtime units without moving module policy into them."""
        self.runtime_supervisor.register(
            RuntimeAdapter(
                runtime_id="audio.micro",
                description="Native MICRO transport shared by Tune, Hard Gate and Voice FX",
                required=self._runtime_requires_audio_micro,
                start=self._runtime_start_audio_micro,
                stop=self._runtime_stop_audio_micro,
                health=self._runtime_health_audio_micro,
                start_timeout=2.5,
                stop_timeout=1.8,
            )
        )
        self.runtime_supervisor.register(
            RuntimeAdapter(
                runtime_id="soundboard",
                description="Soundboard PipeWire bus and routes",
                required=lambda: self.module_registry.is_enabled("pads"),
                start=self._runtime_start_soundboard,
                stop=self._runtime_stop_soundboard,
                health=self._runtime_health_soundboard,
                start_timeout=1.8,
                stop_timeout=1.4,
            )
        )
        self.runtime_supervisor.register(
            RuntimeAdapter(
                runtime_id="remote.server",
                description="One shared Pads / Voice FX Remote server",
                required=self.module_registry.remote_runtime_required,
                start=self._runtime_start_remote,
                stop=self._runtime_stop_remote,
                health=self._runtime_health_remote,
                start_timeout=5.0,
                stop_timeout=2.8,
            )
        )

    def _runtime_requires_audio_micro(self) -> bool:
        checker = getattr(self.audio_engine, "_native_micro_enabled", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    def _runtime_start_audio_micro(self) -> tuple[bool, str]:
        try:
            runtime_settings = self._audio_runtime_settings()
            writer = getattr(self.audio_engine, "_write_native_micro_state", None)
            ensure = getattr(self.audio_engine, "_ensure_native_micro_engine", None)
            if callable(writer):
                writer(runtime_settings)
            if not callable(ensure):
                return False, "Native MICRO runtime start hook unavailable"
            ok = bool(ensure())
            return ok, "Native MICRO ready" if ok else "Native MICRO failed to start"
        except Exception as exc:
            return False, str(exc)

    def _runtime_stop_audio_micro(self) -> tuple[bool, str]:
        try:
            stop = getattr(self.audio_engine, "_stop_native_micro_engine", None)
            if not callable(stop):
                return False, "Native MICRO runtime stop hook unavailable"
            stop()
            return True, "Native MICRO stopped"
        except Exception as exc:
            return False, str(exc)

    def _runtime_health_audio_micro(self) -> bool:
        checker = getattr(self.audio_engine, "_native_micro_transport_running", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        proc = getattr(self.audio_engine, "_native_micro_proc", None)
        try:
            return bool(proc is not None and proc.poll() is None)
        except Exception:
            return False

    def _runtime_start_soundboard(self) -> tuple[bool, str]:
        if not self.module_registry.is_enabled("pads"):
            return False, "Pads module is disabled"
        self._soundboard_pop_guard_active = True
        try:
            # Keep the private Soundboard bus and its freshly-created loopbacks
            # silent while PipeWire reconnects them.  Recreating module-loopback
            # can otherwise emit a full-scale transient even when no pad plays.
            self._set_soundboard_bus_muted(True)
            self._set_soundboard_route_streams_muted(True)
            self._reconcile_soundboard_startup_routes()
            ok = self._runtime_health_soundboard()
            if ok:
                time.sleep(SOUNDBOARD_RESTART_POST_MUTE_SECONDS)
            self._soundboard_pop_guard_active = False
            if ok:
                # Release the source first while every destination route is still
                # muted, then expose the settled loopbacks a moment later.
                self._set_soundboard_bus_muted(False)
                time.sleep(0.02)
                self._set_soundboard_route_streams_muted(False)
                self._unmute_soundboard_output_streams(force=True)
            else:
                # Never leave a partial failed start permanently muted; the
                # supervisor will report the failed health state separately.
                self._set_soundboard_bus_muted(False)
                self._set_soundboard_route_streams_muted(False)
            return ok, "Soundboard runtime ready (pop guard released)" if ok else "Soundboard bus did not become ready"
        except Exception as exc:
            self._soundboard_pop_guard_active = False
            try:
                self._set_soundboard_bus_muted(False)
                self._set_soundboard_route_streams_muted(False)
            except Exception:
                pass
            return False, str(exc)

    def _runtime_stop_soundboard(self) -> tuple[bool, str]:
        try:
            # Fade the Soundboard path to digital silence before tearing down its
            # PipeWire bus.  This mute targets only Soundboard's own routes, not
            # GAME/MEDIA or the user's physical output.
            self._set_soundboard_route_streams_muted(True)
            self._set_soundboard_bus_muted(True)
            time.sleep(SOUNDBOARD_RESTART_PRE_MUTE_SECONDS)
            try:
                self.soundboard_service.stop_all()
            except Exception:
                pass
            self._unload_soundboard_monitor_loopbacks()
            self._unload_modules_matching(
                lambda line: (
                    "module-null-sink" in line.lower()
                    and "sink_name=soundboard" in line.lower()
                )
            )
            if self._micro_hard_gate_uses_native_engine():
                self._refresh_native_micro_hard_gate_runtime()
            return True, "Soundboard runtime stopped (pop guard armed)"
        except Exception as exc:
            return False, str(exc)

    def _runtime_health_soundboard(self) -> bool:
        try:
            return bool(
                self._sink_exists("soundboard")
                and self._source_exists("soundboard.monitor")
            )
        except Exception:
            return False

    def _runtime_start_remote(self) -> tuple[bool, str]:
        config = self._remote_runtime_config()
        return ensure_remote_server(config, python_executable=sys.executable)

    def _runtime_stop_remote(self) -> tuple[bool, str]:
        try:
            count = stop_remote_server(self._remote_runtime_config())
            return True, f"Remote server stopped ({count} process{'es' if count != 1 else ''})"
        except Exception as exc:
            return False, str(exc)

    def _runtime_health_remote(self) -> bool:
        if not self.module_registry.remote_runtime_required():
            return False
        try:
            return bool(remote_server_reachable(self._remote_runtime_config()))
        except Exception:
            return False

    def _apply_module_runtime_settings(self) -> tuple[bool, str]:
        """Apply enabled/disabled DSP state without persisting or restarting Glass."""
        try:
            self.audio_engine.apply_settings(self._audio_runtime_settings())
            return True, "Audio module state applied"
        except Exception as exc:
            return False, str(exc)

    def _module_transition_worker(
        self,
        module_id: str,
        target_enabled: bool,
        previous_enabled: bool,
    ) -> None:
        ok = True
        messages: list[str] = []
        try:
            manifest = self.module_registry.manifest(module_id)
            if manifest is not None and not manifest.builtin:
                if target_enabled:
                    self.external_module_host.start(module_id)
                    messages.append("External module handshake accepted")
                else:
                    self.external_module_host.stop(module_id)
                    messages.append("External module process stopped")
            elif module_id in {"eq", "micro_tune", "hard_gate", "voice_fx"}:
                applied, detail = self._apply_module_runtime_settings()
                ok = bool(applied)
                messages.append(detail)
            else:
                messages.append("No DSP reapply required")
            if ok and (manifest is None or manifest.builtin):
                runtime_ok, runtime_detail = self.runtime_supervisor.reconcile_module(module_id)
                ok = bool(runtime_ok)
                messages.append(runtime_detail)
        except Exception as exc:
            ok = False
            messages.append(str(exc))

        if not ok:
            # Enabling/disabling is transactional from the user's point of view:
            # if the runtime cannot reach a healthy state, restore the previous
            # desired state and best-effort reconcile the old configuration.
            self.module_registry.set_enabled(module_id, previous_enabled)
            try:
                if manifest is not None and not manifest.builtin:
                    self.external_module_host.stop(module_id)
                if module_id in {"eq", "micro_tune", "hard_gate", "voice_fx"}:
                    self._apply_module_runtime_settings()
                if manifest is None or manifest.builtin:
                    self.runtime_supervisor.reconcile_module(module_id)
            except Exception:
                pass

        self._module_transition_result.emit(
            module_id,
            bool(target_enabled),
            bool(ok),
            "; ".join(item for item in messages if item),
        )

    def _finish_module_transition(
        self,
        module_id: str,
        target_enabled: bool,
        ok: bool,
        message: str,
    ) -> None:
        wanted = str(module_id or "").strip().lower()
        manifest = self.module_registry.manifest(wanted)
        label = manifest.name if manifest is not None else wanted
        if ok:
            self.module_manager.finish(wanted)
            self._save_timer.start()
            state = "enabled" if target_enabled else "disabled"
            self.status_changed.emit(f"Module {label}: {state} · runtime reconciled")
        else:
            self.module_manager.fail(wanted, message or "Runtime transition failed")
            self.status_changed.emit(f"Module {label}: transition failed — {message}")
        self.module_state_changed.emit(wanted)
        if wanted in {"pads", "voice_fx", "remote"}:
            self.module_state_changed.emit("remote")
            self._queue_remote_watchdog_check(force=True)

    def _module_runtime_restart_worker(self, module_id: str) -> None:
        wanted = str(module_id or "").strip().lower()
        manifest = self.module_registry.manifest(wanted)
        runtime_ids = self.runtime_supervisor.runtimes_for_module(wanted)
        ok = True
        messages: list[str] = []
        if manifest is not None and not manifest.builtin:
            try:
                self.external_module_host.restart(wanted)
                messages.append("External module restarted · handshake accepted")
            except Exception as exc:
                ok = False
                messages.append(str(exc))
        elif runtime_ids:
            for runtime_id in runtime_ids:
                runtime_ok, detail = self.runtime_supervisor.restart(runtime_id)
                ok = bool(runtime_ok)
                messages.append(f"{runtime_id}: {detail}")
                if not ok:
                    break
        else:
            ok, detail = self._apply_module_runtime_settings()
            messages.append(detail)
        if ok and wanted in {"pads", "voice_fx", "remote"}:
            remote_ok, detail = self.runtime_supervisor.reconcile("remote.server")
            ok = bool(remote_ok)
            messages.append(f"remote.server: {detail}")
        self._module_restart_result.emit(wanted, bool(ok), "; ".join(messages))

    def restart_module_runtime(self, module_id: str) -> bool:
        wanted = str(module_id or "").strip().lower()
        manifest = self.module_registry.manifest(wanted)
        if manifest is None or not self.module_registry.is_installed(wanted):
            return False
        if not manifest.builtin and not self.module_registry.is_enabled(wanted):
            return False
        if not self.module_manager.begin(wanted, ModulePhase.STARTING):
            self.status_changed.emit(f"Module {wanted}: operation already in progress")
            return False
        self.module_state_changed.emit(wanted)
        threading.Thread(
            target=self._module_runtime_restart_worker,
            args=(wanted,),
            name=f"ksh-runtime-restart-{wanted}",
            daemon=True,
        ).start()
        return True

    def _finish_module_runtime_restart(self, module_id: str, ok: bool, message: str) -> None:
        wanted = str(module_id or "").strip().lower()
        if ok:
            self.module_manager.finish(wanted)
            self._module_restart_counts[wanted] = self.module_restart_count(wanted) + 1
            count = self.module_restart_count(wanted)
            self.status_changed.emit(f"Module {wanted}: runtime ready · restart #{count}")
        else:
            self.module_manager.fail(wanted, message or "Runtime restart failed")
            self.status_changed.emit(f"Module {wanted}: runtime restart failed — {message}")
        self.module_state_changed.emit(wanted)

    def _runtime_reconcile_all_worker(self) -> None:
        ok, message = self.runtime_supervisor.reconcile_all()
        self._runtime_reconcile_result.emit(bool(ok), message)

    def _queue_runtime_reconcile_all(self) -> None:
        threading.Thread(
            target=self._runtime_reconcile_all_worker,
            name="ksh-runtime-reconcile",
            daemon=True,
        ).start()

    def _finish_runtime_reconcile(self, ok: bool, message: str) -> None:
        if not ok and message:
            self.status_changed.emit(f"Runtime supervisor warning — {message}")
        for module_id in ("micro_tune", "hard_gate", "voice_fx", "pads", "remote"):
            self.module_state_changed.emit(module_id)

    def set_module_enabled(self, module_id: str, enabled: bool) -> bool:
        wanted = str(module_id or "").strip().lower()
        previous_enabled = bool(self.module_registry.is_enabled(wanted))
        target_enabled = bool(enabled)
        if previous_enabled == target_enabled:
            return True

        manifest = self.module_registry.manifest(wanted)

        if target_enabled and manifest is not None and not manifest.builtin:
            if not self.external_module_supported(wanted):
                self.status_changed.emit(
                    f"Module {wanted}: no compatible runner in module.json"
                )
                return False

        ok, message = self.module_manager.request_enable(wanted, target_enabled)
        if not ok:
            manifest = self.module_registry.manifest(wanted)
            label = manifest.name if manifest is not None else wanted
            self.status_changed.emit(f"Module {label}: {message}")
            return False

        phase = ModulePhase.STARTING if target_enabled else ModulePhase.STOPPING
        if not self.module_manager.begin(wanted, phase):
            self.module_registry.set_enabled(wanted, previous_enabled)
            self.status_changed.emit(f"Module {wanted}: operation already in progress")
            return False

        manifest = self.module_registry.manifest(wanted)
        label = manifest.name if manifest is not None else wanted
        self.status_changed.emit(
            f"Module {label}: {'starting' if target_enabled else 'stopping'}…"
        )
        threading.Thread(
            target=self._module_transition_worker,
            args=(wanted, target_enabled, previous_enabled),
            name=f"ksh-module-transition-{wanted}",
            daemon=True,
        ).start()
        return True

    def install_module_archive(self, archive_path: str) -> bool:
        """Install package payload without executing third-party code."""
        path = Path(archive_path).expanduser()

        try:
            manifest = self.module_package_installer.inspect_archive(path)

            if self.module_registry.is_installed(manifest.module_id):
                operation = "updated"
                self.module_package_installer.update_archive(path)
            else:
                operation = "installed"
                self.module_package_installer.install_archive(path)

        except Exception as exc:
            self.status_changed.emit(
                f"Module package rejected — {exc}"
            )
            return False

        self._save_timer.start()

        self.status_changed.emit(
            f"Module {manifest.name} v{manifest.version} "
            f"{operation} (disabled; enable explicitly to run trusted code)"
        )

        self.module_state_changed.emit(manifest.module_id)
        return True

    def uninstall_module(self, module_id: str) -> bool:
        wanted = str(module_id or "").strip().lower()
        if not self.module_manager.can_uninstall(wanted):
            self.status_changed.emit(
                "Module uninstall requires a removable module that is installed and disabled"
            )
            return False
        ok = bool(self.module_package_installer.uninstall(wanted))
        if ok:
            self._save_timer.start()
            self.status_changed.emit(f"Module {wanted}: uninstalled")
            self.module_state_changed.emit(wanted)
        else:
            self.status_changed.emit(f"Module {wanted}: uninstall failed")
        return ok

    def create_remote_pairing_code(self) -> tuple[bool, str, str]:
        if not self.module_registry.remote_runtime_required():
            return False, "", "Remote has no enabled Pads or Voice FX surface"
        try:
            ok, message = self.runtime_supervisor.reconcile("remote.server")
        except Exception as exc:
            return False, "", str(exc)
        if not ok:
            return False, "", message

        pin = f"{secrets.randbelow(1_000_000):06d}"
        pairing_path = CONFIG_DIR / "soundboard_pairing.json"
        try:
            pairing_path.parent.mkdir(parents=True, exist_ok=True)
            pairing_path.write_text(
                json.dumps(
                    {
                        "pin": pin,
                        "expires_at": time.time() + 300,
                        "created_at": time.time(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                pairing_path.chmod(0o600)
            except Exception:
                pass
        except Exception as exc:
            return False, "", f"Could not write pairing code: {exc}"
        return True, pin, message

    def _queue_remote_watchdog_check(self, *, force: bool = False) -> None:
        if not self._remote_watchdog_enabled or self._remote_watchdog_shutdown:
            if self._remote_watchdog_timer.isActive():
                self._remote_watchdog_timer.stop()
            return

        runtime_required = bool(self.module_registry.remote_runtime_required())
        if not runtime_required and not force:
            # Fully idle means fully idle: no detached server and no 4-second
            # polling timer merely to rediscover that there is nothing to serve.
            if self._remote_watchdog_timer.isActive():
                self._remote_watchdog_timer.stop()
            self.module_manager.mark_runtime_state("remote", "idle")
            self.remote_status_changed.emit("Idle")
            return

        if runtime_required and not self._remote_watchdog_timer.isActive():
            self._remote_watchdog_timer.start()

        if self._remote_watchdog_inflight:
            return

        self._remote_watchdog_inflight = True
        thread = threading.Thread(
            target=self._remote_watchdog_worker,
            name="ksh-remote-watchdog",
            daemon=True,
        )
        thread.start()

    def _remote_watchdog_worker(self) -> None:
        if self._remote_watchdog_shutdown:
            self._remote_watchdog_result.emit("Offline", "Glass is shutting down")
            return

        try:
            ok, message = self.runtime_supervisor.reconcile("remote.server")
            if self._remote_watchdog_shutdown:
                # A watchdog worker can overlap shutdown.  Never let a late
                # reconciliation recreate the shared Remote server while
                # Glass is going away.
                try:
                    self._runtime_stop_remote()
                except Exception:
                    pass
                self._remote_watchdog_result.emit("Offline", "Glass is shutting down")
                return

            status = self.runtime_supervisor.status("remote.server")
            if not self.module_registry.remote_runtime_required():
                self._remote_watchdog_result.emit("Idle", message or "No enabled Remote surfaces")
            elif ok and status.healthy:
                self._remote_watchdog_result.emit("Online", message or "Remote server healthy")
            elif status.state == "starting":
                self._remote_watchdog_result.emit("Recovering", message or "Remote starting")
            else:
                self._remote_watchdog_result.emit("Offline", message or status.detail)
        except Exception as exc:
            self._remote_watchdog_result.emit("Offline", str(exc))

    def _apply_remote_watchdog_result(self, state: str, message: str) -> None:
        self._remote_watchdog_inflight = False
        if self._remote_watchdog_shutdown:
            return

        normalized = str(state or "Offline").strip() or "Offline"
        if normalized == "Online":
            self.module_manager.mark_runtime_state("remote", "running")
        elif normalized == "Idle":
            self.module_manager.mark_runtime_state("remote", "idle")
            # Reconcile has already stopped the shared Remote server. No surface
            # means there is nothing useful to poll until a module transition
            # explicitly wakes the watchdog again.
            if self._remote_watchdog_timer.isActive():
                self._remote_watchdog_timer.stop()
        elif normalized == "Recovering":
            self.module_manager.mark_runtime_state("remote", "starting")
        else:
            self.module_manager.mark_runtime_state("remote", "error")
        self.remote_status_changed.emit(normalized)
        if normalized == "Offline" and message:
            self.status_changed.emit(f"Remote recovery error — {message}")

    def _audio_runtime_settings(self):
        """Return a non-persistent settings snapshot honoring enabled modules."""
        runtime = copy.deepcopy(self.settings)
        eq_enabled = self.module_enabled("eq")
        tune_enabled = self.module_enabled("micro_tune")
        hard_gate_enabled = self.module_enabled("hard_gate")
        voice_fx_enabled = self.module_enabled("voice_fx")
        for channel in getattr(runtime, "channels", []):
            if not eq_enabled:
                channel.eq_profiles = [EqProfile.default()]
                channel.selected_eq_profile = "Default"
            if str(getattr(channel, "key", "") or "") == "micro":
                if not hard_gate_enabled:
                    channel.hard_gate_enabled = False
                if not tune_enabled:
                    channel.micro_processing_enabled = False
                if not voice_fx_enabled:
                    channel.micro_voice_fx_active = []
                    channel.micro_voice_fx_enabled = False
                    channel.micro_custom_fx_enabled = False
        return runtime

    def apply_startup_settings(self) -> None:
        try:
            self._ensure_glass_virtual_audio_buses()
            self._repair_invalid_micro_input_target()
            runtime_settings = self._audio_runtime_settings()
            if self._preserve_audio_attach:
                # UI/Core handoff: the existing PipeWire graph and detached
                # audio workers are intentionally left alive by the previous
                # Glass instance. Reattach to their state instead of rebuilding
                # routes that are already carrying live GAME/MEDIA/CHAT audio.
                refresher = getattr(self.audio_engine, "refresh_native_micro_state", None)
                if callable(refresher):
                    refresher(runtime_settings)
                self.status_changed.emit("Audio routing handoff attached — live routes preserved")
                return

            self.audio_engine.apply_settings(runtime_settings)
            self._reapply_saved_app_routes()
            self.apply_glass_runtime_routes()
            # PipeWire nodes can still be settling during the first startup
            # pass. Reconcile Soundboard routes again after the event loop is
            # alive so a configured Soundboard -> MICRO send cannot silently
            # miss its native capture route until the next app restart.
            self._schedule_soundboard_startup_reconcile()
            self.status_changed.emit(self.audio_engine.status_text())
        except Exception as exc:
            self.status_changed.emit(f"Audio backend startup error — {exc}")

    def _schedule_soundboard_startup_reconcile(self) -> None:
        if not self.module_enabled("pads"):
            return
        # Two bounded idempotent passes are intentional: the first catches the
        # normal short PipeWire startup race, the second catches a slower USB /
        # native MICRO bring-up. No per-pad pactl work is introduced.
        for delay_ms in (280, 950):
            QTimer.singleShot(delay_ms, self._reconcile_soundboard_startup_routes)

    def _reconcile_soundboard_startup_routes(self) -> None:
        try:
            output_key = self._soundboard_output_config()
            send_to_micro = self._soundboard_send_to_micro_config()
            self._ensure_glass_virtual_audio_buses()
            if not self._ensure_soundboard_bus():
                return

            self._load_soundboard_output_loopback(output_key)
            self._load_soundboard_micro_loopback(send_to_micro)
            if output_key == "return-mic":
                self._reconcile_soundboard_mic_out_final()

            # Native MICRO consumes soundboard.monitor directly from the state
            # file. Force a fresh state after the Soundboard bus exists so the
            # send is present even if the first startup apply happened too early.
            if send_to_micro and self._micro_hard_gate_uses_native_engine():
                self._refresh_native_micro_hard_gate_runtime()
        except Exception as exc:
            self.status_changed.emit(f"Soundboard startup reconcile warning — {exc}")

    def _repair_invalid_micro_input_target(self) -> None:
        """Keep an offline physical MICRO preference; reject unsafe loops only.

        Device absence is NOT configuration corruption. The native MICRO
        source resolver/watchdog handles absence as silence and reconnects
        when the selected device returns.
        """
        channel = self._find_channel("micro")
        if channel is None:
            return

        current = str(getattr(channel, "primary_target", "") or "").strip()
        if not current:
            return

        validator = getattr(self.audio_engine, "_is_valid_micro_input_source_name", None)
        if not callable(validator):
            return  # Never rewrite saved preferences if validation is unavailable.

        if not validator(current):
            # Still prevent feedback loops through the exported 'micro' source
            # or PipeWire monitor sources. This is NOT a USB unplug event.
            channel.primary_target = ""
            self.settings_store.save(self.settings)
            self.status_changed.emit("Unsafe MICRO input selection cleared")
            return

        available = {name for _label, name in self.available_input_targets()}
        if current not in available:
            # Keep the exact chosen RODE/USB source in settings across hotplug.
            # An unavailable source is rendered as offline, not auto-replaced.
            self.status_changed.emit("MICRO input offline — selection preserved")

    def cleanup_glass_audio_runtime(self) -> None:
        # PipeWireAudioEngine.shutdown() owns the audio processes it created.
        # The Remote is the only detached process owned directly by Glass.
        # Do not use broad pkill patterns here: they can match unrelated audio.
        try:
            stop_remote_server(RemoteRuntimeConfig.from_environment(PROJECT_ROOT))
        except Exception:
            pass

        try:
            self._unload_modules_matching(
                lambda line: (
                    "module-loopback" in line.lower()
                    and (
                        "ksh_mic_physical" in line.lower()
                        or "k-sound-hub-soundboard" in line.lower()
                        or "k-sounds hub mic output monitor" in line.lower()
                        or "k-sound-hub-return-mic-micro" in line.lower()
                        or "k-sound-hub-micro-inject" in line.lower()
                        or "source=soundboard.monitor" in line.lower()
                        or "sink=micro_bus" in line.lower()
                        or "sink=retour" in line.lower()
                    )
                )
            )
        except Exception:
            pass

    def shutdown(self, *, preserve_audio_routing: bool = False) -> None:
        try:
            self._micro_source_watchdog_timer.stop()
        except Exception:
            pass
        self._external_watchdog_timer.stop()
        self.external_module_host.stop_all()
        self._module_remote_path.unlink(
            missing_ok=True
        )
        self._remote_watchdog_shutdown = True
        try:
            self._remote_watchdog_timer.stop()
        except Exception:
            pass

        try:
            self._autosave()
        except Exception:
            pass

        try:
            self.soundboard_service.shutdown()
        except Exception:
            pass

        try:
            self.ipc_server.stop()
        except Exception:
            pass

        try:
            self._stop_micro_hard_gate_monitor()
        except Exception:
            pass

        try:
            self._stop_realtime_volume_worker()
        except Exception:
            pass

        if preserve_audio_routing:
            # The UI owns meter polling, not the routing graph. Stop disposable
            # probes so the next UI cannot duplicate them, while deliberately
            # leaving sinks, loopbacks, native MICRO and Remote in place.
            try:
                stop_meters = getattr(self.audio_engine, "stop_meter_probes", None)
                if callable(stop_meters):
                    stop_meters()
            except Exception:
                pass
            return

        try:
            self.audio_engine.shutdown()
        except Exception:
            pass

        try:
            self.cleanup_glass_audio_runtime()
        except Exception:
            pass

    def _normalize_channel_key(self, value: str) -> str:
        raw = str(value or "").strip().lower().replace(" ", "-")
        return GLASS_CHANNEL_KEY_ALIASES.get(raw, raw)

    def _find_channel(self, channel_key: str):
        key = self._normalize_channel_key(channel_key)
        for channel in self.settings.channels:
            if channel.key == key:
                return channel
        return None

    def channel_state(self, channel_key: str) -> tuple[int, bool] | None:
        channel = self._find_channel(channel_key)
        if channel is None:
            return None
        return int(channel.volume), bool(channel.muted)

    def channel_stereo_width(self, channel_key: str) -> int:
        channel = self._find_channel(channel_key)
        if channel is None:
            return 100
        try:
            value = int(getattr(channel, "stereo_width", 100))
        except Exception:
            value = 100
        return max(0, min(100, value))

    def set_channel_stereo_width(self, channel_key: str, value: int) -> bool:
        channel = self._find_channel(channel_key)
        if channel is None:
            return False

        try:
            numeric = int(value)
        except Exception:
            numeric = 100

        # UI uses 5% steps. Clamp and normalize here too so config stays clean.
        numeric = max(0, min(100, int(round(numeric / 5.0) * 5)))

        if int(getattr(channel, "stereo_width", 100)) == numeric:
            return True

        channel.stereo_width = numeric
        self.settings_store.save(self.settings)

        try:
            self.audio_engine.apply_channel(self._audio_runtime_settings(), channel.key)
        except Exception as exc:
            self.status_changed.emit(f"Stereo width warning — {exc}")

        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("", channel.key.upper()))[1]
        mode = "Mono" if numeric == 0 else "Stereo" if numeric == 100 else "Narrow stereo"
        self.overlay_message_requested.emit(f"↔ {label} {mode} {numeric}%", False)
        return True

    def meter_levels(self, channel_key: str) -> tuple[float, float]:
        if not self._metering_enabled:
            return 0.0, 0.0

        try:
            key = self._normalize_channel_key(channel_key)
            left, right = self.audio_engine.meter_levels(key)

            # Display the amplitude measured by the engine as-is. A second
            # UI-only volume gain hid legitimate signals below -60 dBFS.
            channel = self._find_channel(key)
            if channel is not None and bool(getattr(channel, "muted", False)):
                left, right = 0.0, 0.0

            return max(0.0, min(1.0, float(left))), max(0.0, min(1.0, float(right)))
        except Exception as exc:
            self.status_changed.emit(f"Meter read error on {channel_key} — {exc}")
            return 0.0, 0.0

    def meter_levels_batch(
        self,
        channel_keys,
    ) -> dict[str, tuple[float, float]]:
        # Read all requested meters once, then apply Glass-only display state.
        if not self._metering_enabled:
            return {
                str(key or "").strip(): (0.0, 0.0)
                for key in channel_keys
                if str(key or "").strip()
            }

        requested = tuple(
            str(key or "").strip()
            for key in channel_keys
            if str(key or "").strip()
        )
        normalized = tuple(
            self._normalize_channel_key(key)
            for key in requested
        )

        engine_batch = getattr(
            self.audio_engine,
            "meter_levels_batch",
            None,
        )

        try:
            if callable(engine_batch):
                raw = engine_batch(normalized)
            else:
                raw = {
                    key: self.audio_engine.meter_levels(key)
                    for key in normalized
                }
        except Exception as exc:
            self.status_changed.emit(
                f"Meter batch read error — {exc}"
            )
            raw = {}

        result: dict[str, tuple[float, float]] = {}

        for original, key in zip(requested, normalized):
            try:
                left, right = raw.get(key, (0.0, 0.0))
                channel = self._find_channel(key)

                # Same native-amplitude policy as meter_levels(). Muting
                # still hides that channel without changing its audio route.
                if channel is not None and bool(getattr(channel, "muted", False)):
                    left, right = 0.0, 0.0

                result[original] = (
                    max(0.0, min(1.0, float(left))),
                    max(0.0, min(1.0, float(right))),
                )
            except Exception:
                result[original] = (0.0, 0.0)

        return result

    def set_metering_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._metering_enabled == enabled:
            return

        self._metering_enabled = enabled
        if not enabled:
            self.audio_engine.stop_meter_probes()

    def list_app_streams(self) -> list:
        try:
            self._app_route_sink_info_cache = None
            self._app_route_clients_cache = None
            streams = list(self.audio_engine.list_sink_inputs())

            for stream in streams:
                try:
                    friendly = self._friendly_display_name_for_stream(stream)
                    if friendly:
                        stream.display_name = friendly
                except Exception:
                    pass

            return streams
        except Exception as exc:
            self.status_changed.emit(f"Apps list error — {exc}")
            return []


    def _app_route_prop_value(self, line: str) -> str:
        return str(line.split("=", 1)[1] if "=" in line else "").strip().strip('"')

    def _pactl_route_text(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["pactl", *args],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout or ""

    def _app_route_sink_info_by_id(self) -> dict[int, dict[str, str]]:
        cached = getattr(self, "_app_route_sink_info_cache", None)
        if isinstance(cached, dict):
            return cached

        blocks: dict[int, dict[str, str]] = {}
        current_id: int | None = None

        for raw_line in self._pactl_route_text("list", "sink-inputs").splitlines():
            line = raw_line.strip()

            if raw_line.startswith("Sink Input #"):
                try:
                    current_id = int(raw_line.split("#", 1)[1].strip())
                except Exception:
                    current_id = None
                if current_id is not None:
                    blocks[current_id] = {}
                continue

            if current_id is None:
                continue

            info = blocks.setdefault(current_id, {})

            if line.startswith("Client:"):
                info["client_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("application.process.id = "):
                info["process_id"] = self._app_route_prop_value(line)
            elif line.startswith("application.name = "):
                info["app_name"] = self._app_route_prop_value(line)
            elif line.startswith("application.process.binary = "):
                info["binary_name"] = self._app_route_prop_value(line)
            elif line.startswith("media.name = "):
                info["media_name"] = self._app_route_prop_value(line)
            elif line.startswith("media.role = "):
                info["media_role"] = self._app_route_prop_value(line)
            elif line.startswith("node.name = "):
                info["node_name"] = self._app_route_prop_value(line)
            elif line.startswith("node.loop.name = "):
                info["node_loop_name"] = self._app_route_prop_value(line)

        self._app_route_sink_info_cache = blocks
        return blocks

    def _app_route_clients_by_id(self) -> dict[str, dict[str, str]]:
        cached = getattr(self, "_app_route_clients_cache", None)
        if isinstance(cached, dict):
            return cached

        clients: dict[str, dict[str, str]] = {}
        current_id = ""

        for raw_line in self._pactl_route_text("list", "clients").splitlines():
            line = raw_line.strip()

            if raw_line.startswith("Client #"):
                current_id = raw_line.split("#", 1)[1].strip()
                clients[current_id] = {}
                continue

            if not current_id:
                continue

            info = clients.setdefault(current_id, {})

            if line.startswith("application.process.id = "):
                info["process_id"] = self._app_route_prop_value(line)
            elif line.startswith("application.name = "):
                info["app_name"] = self._app_route_prop_value(line)
            elif line.startswith("application.process.binary = "):
                info["binary_name"] = self._app_route_prop_value(line)

        self._app_route_clients_cache = clients
        return clients

    def _app_route_cmdline_for_pid(self, pid: str) -> str:
        pid = str(pid or "").strip()
        if not pid.isdigit():
            return ""

        cache = getattr(self, "_app_route_cmdline_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._app_route_cmdline_cache = cache

        if pid in cache:
            return cache[pid]

        try:
            raw = Path("/proc", pid, "cmdline").read_bytes()
            value = raw.decode("utf-8", "replace").replace("\x00", " ").strip()
        except Exception:
            value = ""

        cache[pid] = value
        return value


    def _process_cmdline_for_stream(self, stream) -> str:
        try:
            stream_id = int(getattr(stream, "stream_id", 0))
        except Exception:
            return ""

        info = dict(self._app_route_sink_info_by_id().get(stream_id, {}))

        client_id = str(info.get("client_id") or "").strip()
        if client_id:
            client_info = self._app_route_clients_by_id().get(client_id, {})
            for key in ("process_id", "app_name", "binary_name"):
                if not info.get(key) and client_info.get(key):
                    info[key] = client_info[key]

        return self._app_route_cmdline_for_pid(str(info.get("process_id") or ""))

    def _friendly_display_name_for_stream(self, stream) -> str:
        original = str(getattr(stream, "display_name", "") or "")

        # Keep internal soundboard labels stable.
        if original.upper().startswith("SOUNDBOARD"):
            return original

        return stream_display_name_from_data(
            cmdline=self._process_cmdline_for_stream(stream),
            display_name=original,
            app_name=str(getattr(stream, "app_name", "") or ""),
            binary_name=str(getattr(stream, "binary_name", "") or ""),
            media_name=str(getattr(stream, "media_name", "") or ""),
            node_name=str(getattr(stream, "node_name", "") or ""),
        )


    def _process_route_rule_for_stream(self, stream) -> str:
        try:
            stream_id = int(getattr(stream, "stream_id", 0))
        except Exception:
            return ""

        info = dict(self._app_route_sink_info_by_id().get(stream_id, {}))

        client_id = str(info.get("client_id") or "").strip()
        if client_id:
            client_info = self._app_route_clients_by_id().get(client_id, {})
            for key in ("process_id", "app_name", "binary_name"):
                if not info.get(key) and client_info.get(key):
                    info[key] = client_info[key]

        cmdline = self._app_route_cmdline_for_pid(str(info.get("process_id") or ""))
        return process_route_rule_from_cmdline(cmdline)

    def _role_channel_for_stream(self, stream) -> str:
        try:
            stream_id = int(getattr(stream, "stream_id", 0))
        except Exception:
            return ""

        info = self._app_route_sink_info_by_id().get(stream_id, {})
        return channel_for_media_role(str(info.get("media_role") or ""))

    def _move_app_stream_if_needed(self, stream, channel_key: str) -> bool:
        key = self._normalize_channel_key(channel_key)
        if key not in APP_ROUTE_KEYS:
            return False

        try:
            stream_id = int(getattr(stream, "stream_id", 0))
        except Exception:
            return False

        if str(getattr(stream, "sink_name", "") or "").strip().lower() == key:
            return True

        return bool(self.audio_engine.move_sink_input_to_channel(stream_id, key))

    def _autosort_app_streams_by_role(self, streams, handled_stream_ids: set[int]) -> None:
        if not bool(getattr(self.settings, "auto_sort_app_streams_by_role", True)):
            return

        if self.audio_engine is None:
            return

        for stream in streams:
            try:
                stream_id = int(getattr(stream, "stream_id", 0))
            except Exception:
                continue

            if stream_id in handled_stream_ids:
                continue

            target = self._role_channel_for_stream(stream)
            if target not in {"game", "chat", "media"}:
                continue

            if self._move_app_stream_if_needed(stream, target):
                handled_stream_ids.add(stream_id)


    def _stream_rule_for(self, stream) -> str:
        proc_rule = self._process_route_rule_for_stream(stream)
        if proc_rule:
            return proc_rule

        display_name = str(getattr(stream, "display_name", "") or "")
        media_name = str(getattr(stream, "media_name", "") or "").strip()
        node_name = str(getattr(stream, "node_name", "") or "").strip()
        binary_name = str(getattr(stream, "binary_name", "") or "").strip()
        app_name = str(getattr(stream, "app_name", "") or "").strip()

        # Soundboard streams are better identified by media/node names.
        if display_name.upper().startswith("SOUNDBOARD"):
            if media_name:
                return f"media:{media_name}"
            if node_name:
                return f"node:{node_name}"

        if binary_name:
            return f"bin:{binary_name}"
        if app_name:
            return f"app:{app_name}"
        if media_name:
            return f"media:{media_name}"
        if node_name:
            return f"node:{node_name}"
        return ""

    def _stream_matches_rule(self, stream, rule: str) -> bool:
        rule = str(rule or "").strip()
        if not rule:
            return False
        if rule.startswith("proc:"):
            return self._process_route_rule_for_stream(stream) == rule
        if rule.startswith("bin:"):
            return bool(getattr(stream, "binary_name", "")) and stream.binary_name == rule[4:]
        if rule.startswith("app:"):
            return bool(getattr(stream, "app_name", "")) and stream.app_name == rule[4:]
        if rule.startswith("media:"):
            return bool(getattr(stream, "media_name", "")) and stream.media_name == rule[6:]
        if rule.startswith("node:"):
            return bool(getattr(stream, "node_name", "")) and stream.node_name == rule[5:]
        return False

    def _remember_app_route(self, stream, channel_key: str) -> None:
        target = self._find_channel(channel_key)
        if target is None:
            return

        rule = self._stream_rule_for(stream)
        if not rule:
            return

        rules_to_remove = {rule}
        if rule.startswith("proc:prismlauncher:minecraft"):
            rules_to_remove.update({"bin:java", "app:java", "node:java", "media:Playback Stream"})

        for channel in self.settings.channels:
            rules = list(getattr(channel, "app_rules", []) or [])
            channel.app_rules = [item for item in rules if item not in rules_to_remove]

        target.app_rules = list(getattr(target, "app_rules", []) or [])
        if rule not in target.app_rules:
            target.app_rules.append(rule)

        self._save_timer.start()

    def _reapply_saved_app_routes(self) -> None:
        self._app_route_sink_info_cache = None
        self._app_route_clients_cache = None
        self._app_route_cmdline_cache = {}

        streams = self.list_app_streams()
        if not streams:
            return

        channel_rules: list[tuple[str, list[str]]] = []
        for channel in self.settings.channels:
            channel_key = self._normalize_channel_key(getattr(channel, "key", ""))
            if channel_key not in APP_ROUTE_KEYS:
                continue

            rules = [
                str(rule or "").strip()
                for rule in (getattr(channel, "app_rules", []) or [])
                if str(rule or "").strip()
            ]
            if rules:
                channel_rules.append((channel_key, rules))

        handled_stream_ids: set[int] = set()

        # First pass: precise process/cmdline rules. These must win over old
        # generic app/bin/media rules and over auto-sort role routing.
        for stream in streams:
            try:
                stream_id = int(getattr(stream, "stream_id", 0))
            except Exception:
                continue

            for channel_key, rules in channel_rules:
                proc_rules = [rule for rule in rules if rule.startswith("proc:")]
                if proc_rules and any(self._stream_matches_rule(stream, rule) for rule in proc_rules):
                    if self._move_app_stream_if_needed(stream, channel_key):
                        handled_stream_ids.add(stream_id)
                    break

        # Second pass: legacy/manual app/bin/media/node rules.
        for stream in streams:
            try:
                stream_id = int(getattr(stream, "stream_id", 0))
            except Exception:
                continue

            if stream_id in handled_stream_ids:
                continue

            for channel_key, rules in channel_rules:
                legacy_rules = [rule for rule in rules if not rule.startswith("proc:")]
                if legacy_rules and any(self._stream_matches_rule(stream, rule) for rule in legacy_rules):
                    if self._move_app_stream_if_needed(stream, channel_key):
                        handled_stream_ids.add(stream_id)
                    break

        # Last pass: optional automatic routing for unknown/new streams.
        self._autosort_app_streams_by_role(streams, handled_stream_ids)


    def move_app_stream(self, stream_id: int, channel_key: str) -> bool:
        key = self._normalize_channel_key(channel_key)
        if key not in APP_ROUTE_KEYS:
            self.status_changed.emit(f"Apps route error — unsupported target: {channel_key}")
            return False

        try:
            wanted_id = int(stream_id)
        except Exception:
            return False

        streams = self.list_app_streams()
        chosen = next((stream for stream in streams if int(stream.stream_id) == wanted_id), None)
        if chosen is None:
            self.status_changed.emit("Apps route error — stream disappeared")
            return False

        try:
            ok = bool(self.audio_engine.move_sink_input_to_channel(wanted_id, key))
        except Exception as exc:
            self.status_changed.emit(f"Apps route error — {exc}")
            return False

        if not ok:
            self.status_changed.emit("Apps route error — PipeWire refused move")
            return False

        self._remember_app_route(chosen, key)
        label = APP_ROUTE_LABEL_BY_KEY.get(key, key.upper())
        name = str(getattr(chosen, "display_name", "") or f"Stream {wanted_id}")
        self.overlay_message_requested.emit(f"▤ {name} → {label}", False)
        self.status_changed.emit(f"Apps route: {name} → {label}")
        return True

    def _link_shared_eq_library(self, source_channel=None) -> None:
        # Playback outputs intentionally share one EQ library. MICRO is a
        # separate DSP chain and keeps an independent preset list.
        shared_profiles = None
        if source_channel is not None:
            source_key = self._normalize_channel_key(getattr(source_channel, "key", ""))
            if source_key in APP_ROUTE_KEYS and getattr(source_channel, "eq_profiles", None):
                shared_profiles = source_channel.eq_profiles

        if shared_profiles is None:
            for channel in self.settings.channels:
                key = self._normalize_channel_key(getattr(channel, "key", ""))
                if key in APP_ROUTE_KEYS and getattr(channel, "eq_profiles", None):
                    shared_profiles = channel.eq_profiles
                    break

        if not shared_profiles:
            shared_profiles = [EqProfile.default()]

        valid_names = {str(getattr(profile, "name", "") or "") for profile in shared_profiles}
        default_name = str(getattr(shared_profiles[0], "name", "") or "Default")

        for channel in self.settings.channels:
            key = self._normalize_channel_key(getattr(channel, "key", ""))
            if key not in APP_ROUTE_KEYS:
                continue
            channel.eq_profiles = shared_profiles
            if channel.selected_eq_profile not in valid_names:
                channel.selected_eq_profile = default_name

    def _unique_eq_profile_name(self, channel, wanted: str, *, preserve_current: str | None = None) -> str:
        base = str(wanted or "").strip() or "Preset"
        preserve = str(preserve_current or "").strip()
        existing = {str(getattr(profile, "name", "") or "") for profile in getattr(channel, "eq_profiles", [])}
        if base not in existing or base == preserve:
            return base

        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def _apply_eq_profile_runtime(self, profile_name: str, preferred_channel_key: str | None = None) -> bool:
        profile_name = str(profile_name or "").strip()
        preferred = self._normalize_channel_key(preferred_channel_key or "")
        if preferred == "micro":
            try:
                self._refresh_native_micro_hard_gate_runtime()
                return True
            except Exception as exc:
                self.status_changed.emit(f"MICRO EQ apply error — {exc}")
                return False

        ok = True
        matching_channels = []
        for channel in self.settings.channels:
            key = self._normalize_channel_key(getattr(channel, "key", ""))
            if key not in APP_ROUTE_KEYS:
                continue
            if key != preferred and str(getattr(channel, "selected_eq_profile", "") or "") != profile_name:
                continue
            matching_channels.append(channel)

        matching_channels.sort(
            key=lambda item: self._normalize_channel_key(getattr(item, "key", "")) != preferred
        )

        for channel in matching_channels:
            key = self._normalize_channel_key(getattr(channel, "key", ""))
            try:
                self.audio_engine.apply_channel(self._audio_runtime_settings(), key)
            except Exception as exc:
                ok = False
                self.status_changed.emit(f"EQ apply error on {key} — {exc}")
        return ok

    def eq_profile_names(self, channel_key: str) -> tuple[list[str], str]:
        channel = self._find_channel(channel_key)
        if channel is None:
            return [], ""

        if self._normalize_channel_key(getattr(channel, "key", "")) in APP_ROUTE_KEYS:
            self._link_shared_eq_library(source_channel=channel)
        profiles = list(getattr(channel, "eq_profiles", []) or [])
        names = [str(getattr(profile, "name", "") or "").strip() for profile in profiles]
        names = [name for name in names if name]

        selected = str(getattr(channel, "selected_eq_profile", "") or "").strip()
        if selected not in names and names:
            selected = names[0]
            channel.selected_eq_profile = selected

        return names, selected

    def _eq_profile_for(self, channel_key: str, profile_name: str | None = None):
        channel = self._find_channel(channel_key)
        if channel is None:
            return None, None

        if self._normalize_channel_key(getattr(channel, "key", "")) in APP_ROUTE_KEYS:
            self._link_shared_eq_library(source_channel=channel)
        profiles = list(getattr(channel, "eq_profiles", []) or [])
        if not profiles:
            channel.eq_profiles = [EqProfile.default()]
            profiles = channel.eq_profiles

        wanted = str(profile_name or getattr(channel, "selected_eq_profile", "") or "").strip()
        for profile in profiles:
            if str(getattr(profile, "name", "") or "").strip() == wanted:
                return channel, profile

        channel.selected_eq_profile = str(getattr(profiles[0], "name", "") or "Default")
        return channel, profiles[0]

    def eq_profile_bands(self, channel_key: str, profile_name: str | None = None) -> list[tuple[float, float, float]]:
        _channel, profile = self._eq_profile_for(channel_key, profile_name)
        if profile is None:
            return []

        bands = []
        for band in list(getattr(profile, "bands", []) or []):
            try:
                bands.append((float(band.frequency), float(band.gain_db), float(band.q)))
            except Exception:
                continue
        return bands

    def eq_history_snapshot(self, channel_key: str | None = None) -> dict:
        """Return detached EQ state; MICRO is isolated from playback presets."""
        key = self._normalize_channel_key(channel_key or "")
        if key == "micro":
            channel = self._find_channel("micro")
            if channel is None:
                return {}
            profiles = [profile.to_dict() for profile in list(getattr(channel, "eq_profiles", []) or [])]
            selected = str(getattr(channel, "selected_eq_profile", "") or "")
            return json.loads(json.dumps({"mode": "micro", "profiles": profiles, "selected": selected}, ensure_ascii=False))

        self._link_shared_eq_library()
        profiles = []
        selected = {}
        for channel in self.settings.channels:
            item_key = self._normalize_channel_key(getattr(channel, "key", ""))
            if item_key not in APP_ROUTE_KEYS:
                continue
            candidates = list(getattr(channel, "eq_profiles", []) or [])
            if not profiles and candidates:
                profiles = [profile.to_dict() for profile in candidates]
            selected[item_key] = str(getattr(channel, "selected_eq_profile", "") or "")
        return json.loads(json.dumps({"mode": "playback", "profiles": profiles, "selected": selected}, ensure_ascii=False))

    def restore_eq_history_snapshot(self, snapshot: dict, preferred_channel_key: str | None = None) -> bool:
        if not isinstance(snapshot, dict):
            return False
        raw_profiles = snapshot.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            return False
        profiles = []
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                continue
            try:
                profiles.append(EqProfile.from_dict(raw))
            except Exception:
                continue
        if not profiles:
            return False

        preferred = self._normalize_channel_key(preferred_channel_key or "")
        mode = str(snapshot.get("mode") or ("micro" if preferred == "micro" else "playback"))
        valid_names = {str(getattr(profile, "name", "") or "") for profile in profiles}
        fallback = str(getattr(profiles[0], "name", "") or "Default")

        if mode == "micro" or preferred == "micro":
            channel = self._find_channel("micro")
            if channel is None:
                return False
            before = self.eq_history_snapshot("micro")
            channel.eq_profiles = profiles
            wanted = str(snapshot.get("selected") or getattr(channel, "selected_eq_profile", "") or "")
            channel.selected_eq_profile = wanted if wanted in valid_names else fallback
            self._save_timer.start()
            after = self.eq_history_snapshot("micro")
            if before != after:
                self._queue_eq_history_apply(["micro"], "micro")
            return True

        before = self.eq_history_snapshot()
        selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
        for channel in self.settings.channels:
            item_key = self._normalize_channel_key(getattr(channel, "key", ""))
            if item_key not in APP_ROUTE_KEYS:
                continue
            channel.eq_profiles = profiles
            wanted = str(selected.get(item_key) or getattr(channel, "selected_eq_profile", "") or "")
            channel.selected_eq_profile = wanted if wanted in valid_names else fallback
        self._save_timer.start()
        restored = self.eq_history_snapshot()
        changed_keys = _eq_snapshot_changed_channels(before, restored, APP_ROUTE_KEYS)
        self._queue_eq_history_apply(changed_keys, preferred)
        return True

    def _queue_eq_history_apply(self, channel_keys, preferred_channel_key: str = "") -> None:
        preferred = self._normalize_channel_key(preferred_channel_key or "")
        pending = list(getattr(self, "_pending_eq_history_apply_keys", []))
        for raw_key in channel_keys:
            key = self._normalize_channel_key(raw_key)
            if (key in APP_ROUTE_KEYS or key == "micro") and key not in pending:
                pending.append(key)
        pending.sort(key=lambda key: key != preferred)
        self._pending_eq_history_apply_keys = pending
        if pending:
            self._eq_history_apply_timer.start()

    def _flush_next_eq_history_apply(self) -> None:
        pending = getattr(self, "_pending_eq_history_apply_keys", [])
        if not pending:
            return
        key = pending.pop(0)
        try:
            # Read the latest model here: several rapid Undo/Redo commands can
            # collapse into one correct rebuild instead of replaying stale EQs.
            if key == "micro":
                self._refresh_native_micro_hard_gate_runtime()
            else:
                self.audio_engine.apply_channel(self._audio_runtime_settings(), key)
        except Exception as exc:
            self.status_changed.emit(f"EQ history apply error on {key} — {exc}")
        if pending:
            self._eq_history_apply_timer.start()

    def select_eq_profile(self, channel_key: str, profile_name: str) -> bool:
        channel, profile = self._eq_profile_for(channel_key, profile_name)
        if channel is None or profile is None:
            return False

        channel.selected_eq_profile = str(getattr(profile, "name", "") or profile_name)
        if not self._apply_eq_profile_runtime(channel.selected_eq_profile, channel.key):
            return False

        self._save_timer.start()
        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("≋", channel.name))[1]
        self.overlay_message_requested.emit(f"≋ {label} EQ → {channel.selected_eq_profile}", False)
        return True

    def create_eq_profile(self, channel_key: str, name: str) -> str:
        channel = self._find_channel(channel_key)
        if channel is None:
            return ""

        if self._normalize_channel_key(getattr(channel, "key", "")) in APP_ROUTE_KEYS:
            self._link_shared_eq_library(source_channel=channel)
        profile_name = self._unique_eq_profile_name(channel, name or "New preset")
        profile = EqProfile.default(name=profile_name)
        channel.eq_profiles.append(profile)
        channel.selected_eq_profile = profile.name

        self._save_timer.start()
        self._apply_eq_profile_runtime(profile.name, channel.key)
        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("≋", channel.name))[1]
        self.overlay_message_requested.emit(f"≋ {label} EQ new → {profile.name}", False)
        return profile.name

    def duplicate_eq_profile(self, channel_key: str, profile_name: str, new_name: str) -> str:
        channel, profile = self._eq_profile_for(channel_key, profile_name)
        if channel is None or profile is None:
            return ""

        wanted = new_name or f"{getattr(profile, 'name', 'Preset')} copy"
        clone_name = self._unique_eq_profile_name(channel, wanted)
        clone = EqProfile.from_dict(profile.to_dict())
        clone.name = clone_name
        channel.eq_profiles.append(clone)
        channel.selected_eq_profile = clone.name

        self._save_timer.start()
        self._apply_eq_profile_runtime(clone.name, channel.key)
        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("≋", channel.name))[1]
        self.overlay_message_requested.emit(f"≋ {label} EQ duplicate → {clone.name}", False)
        return clone.name

    def rename_eq_profile(self, channel_key: str, profile_name: str, new_name: str) -> str:
        channel, profile = self._eq_profile_for(channel_key, profile_name)
        if channel is None or profile is None:
            return ""

        old_name = str(getattr(profile, "name", "") or "").strip()
        renamed = self._unique_eq_profile_name(channel, new_name, preserve_current=old_name)
        if not renamed:
            return ""

        channel_key_normalized = self._normalize_channel_key(getattr(channel, "key", ""))
        micro_favorites_before = self.favorite_micro_eq_presets() if channel_key_normalized == "micro" else []
        output_favorites_before = self.favorite_output_eq_presets() if channel_key_normalized in APP_ROUTE_KEYS else []
        profile.name = renamed
        if channel_key_normalized == "micro" and old_name in micro_favorites_before:
            self.settings.favorite_micro_eq_presets = [
                renamed if item == old_name else item for item in micro_favorites_before
            ]
        elif channel_key_normalized in APP_ROUTE_KEYS and old_name in output_favorites_before:
            self.settings.favorite_output_eq_presets = [
                renamed if item == old_name else item for item in output_favorites_before
            ]
        if channel_key_normalized in APP_ROUTE_KEYS:
            for item in self.settings.channels:
                item_key = self._normalize_channel_key(getattr(item, "key", ""))
                if item_key in APP_ROUTE_KEYS and str(getattr(item, "selected_eq_profile", "") or "") == old_name:
                    item.selected_eq_profile = renamed
        channel.selected_eq_profile = renamed
        self._save_timer.start()
        self._apply_eq_profile_runtime(renamed, channel.key)
        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("≋", channel.name))[1]
        self.overlay_message_requested.emit(f"≋ {label} EQ rename → {renamed}", False)
        return renamed

    def delete_eq_profile(self, channel_key: str, profile_name: str) -> str:
        channel, profile = self._eq_profile_for(channel_key, profile_name)
        if channel is None or profile is None:
            return ""

        channel_key_normalized = self._normalize_channel_key(getattr(channel, "key", ""))
        if channel_key_normalized in APP_ROUTE_KEYS:
            self._link_shared_eq_library(source_channel=channel)
        profiles = list(getattr(channel, "eq_profiles", []) or [])
        if len(profiles) <= 1:
            self.status_changed.emit("EQ delete blocked — at least one preset must remain")
            return ""

        deleted_name = str(getattr(profile, "name", "") or profile_name)
        if channel_key_normalized == "micro":
            self.settings.favorite_micro_eq_presets = [
                item for item in self.favorite_micro_eq_presets() if item != deleted_name
            ]
        elif channel_key_normalized in APP_ROUTE_KEYS:
            self.settings.favorite_output_eq_presets = [
                item for item in self.favorite_output_eq_presets() if item != deleted_name
            ]
        remaining = [item for item in profiles if str(getattr(item, "name", "") or "") != deleted_name]
        if not remaining:
            return ""

        replacement = str(getattr(remaining[0], "name", "") or "Default")
        if channel_key_normalized in APP_ROUTE_KEYS:
            for item in self.settings.channels:
                item_key = self._normalize_channel_key(getattr(item, "key", ""))
                if item_key not in APP_ROUTE_KEYS:
                    continue
                item.eq_profiles = remaining
                if str(getattr(item, "selected_eq_profile", "") or "") == deleted_name:
                    item.selected_eq_profile = replacement
        else:
            channel.eq_profiles = remaining
        channel.selected_eq_profile = replacement
        self._save_timer.start()
        self._apply_eq_profile_runtime(replacement, channel.key)
        label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("≋", channel.name))[1]
        self.overlay_message_requested.emit(f"≋ {label} EQ deleted → {deleted_name}", False)
        return replacement

    def set_eq_band_gain(self, channel_key: str, band_index: int, gain_db: float, profile_name: str | None = None) -> bool:
        channel, profile = self._eq_profile_for(channel_key, profile_name)
        if channel is None or profile is None:
            return False

        bands = list(getattr(profile, "bands", []) or [])
        try:
            band = bands[int(band_index)]
        except Exception:
            return False

        try:
            normalized_gain = max(-12.0, min(12.0, round(float(gain_db) * 2.0) / 2.0))
        except Exception:
            return False

        # sliderReleased can follow the final sliderMoved event. Treat the
        # duplicate value as a no-op so one gesture never restarts the audio
        # graph twice.
        if abs(float(getattr(band, "gain_db", 0.0)) - normalized_gain) < 0.001:
            return True
        band.gain_db = normalized_gain

        profile_name = str(getattr(profile, "name", "") or getattr(channel, "selected_eq_profile", ""))
        channel.selected_eq_profile = profile_name
        if not self._apply_eq_profile_runtime(profile_name, channel.key):
            return False

        self._save_timer.start()
        return True

    def _overlay_text(self, channel, hint: str) -> str:
        icon, label = GLASS_CHANNEL_OVERLAY_META.get(channel.key, ("🎚", channel.name))
        if hint == "mute":
            return f"{icon} {label} {'🔇' if channel.muted else '🔊'}"
        return f"{icon} {label} {int(channel.volume)}%"

    def micro_processing_state(self) -> dict:
        channel = self._find_channel("micro")
        if channel is None:
            return {}

        probe = getattr(self.audio_engine, "native_micro_rnnoise_available", None)
        try:
            noise_available = bool(probe()) if callable(probe) else False
        except Exception:
            noise_available = False

        return {
            "processing_enabled": bool(getattr(channel, "micro_processing_enabled", False)),
            "noise_suppression_available": noise_available,
            "input_gain_db": max(-12.0, min(24.0, float(getattr(channel, "micro_input_gain_db", 0.0)))),
            "noise_suppression_enabled": bool(getattr(channel, "micro_noise_suppression_enabled", False)),
            "noise_suppression": max(0, min(100, int(getattr(channel, "micro_noise_suppression", 0)))),
            "gate_enabled": bool(getattr(channel, "micro_gate_enabled", False)),
            "gate_threshold": max(0, min(100, int(getattr(channel, "micro_gate_threshold", 35)))),
            "compressor_enabled": bool(getattr(channel, "micro_compressor_enabled", False)),
            "compressor_amount": max(0, min(100, int(getattr(channel, "micro_compressor_amount", 45)))),
            "deesser_enabled": bool(getattr(channel, "micro_deesser_enabled", False)),
            "deesser_amount": max(0, min(100, int(getattr(channel, "micro_deesser_amount", 35)))),
            "limiter_enabled": bool(getattr(channel, "micro_limiter_enabled", True)),
            "limiter_ceiling_db": max(-12.0, min(0.0, float(getattr(channel, "micro_limiter_ceiling_db", -1.0)))),
            "hard_gate_enabled": bool(getattr(channel, "hard_gate_enabled", False)),
            "hard_gate_threshold": max(0, min(100, int(getattr(channel, "hard_gate_threshold", 12)))),
            "eq_profile": str(getattr(channel, "selected_eq_profile", "Default") or "Default"),
        }

    def micro_processing_activity(self) -> dict:
        reader = getattr(self.audio_engine, "native_micro_processing_activity", None)
        if not callable(reader):
            return {}
        try:
            payload = reader()
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def set_micro_processing_state(self, updates: dict) -> bool:
        if not self.module_enabled("micro_tune"):
            return False
        channel = self._find_channel("micro")
        if channel is None or not isinstance(updates, dict):
            return False

        try:
            if "processing_enabled" in updates:
                channel.micro_processing_enabled = bool(updates["processing_enabled"])
            if "input_gain_db" in updates:
                channel.micro_input_gain_db = max(-12.0, min(24.0, float(updates["input_gain_db"])))
            if "noise_suppression_enabled" in updates:
                channel.micro_noise_suppression_enabled = bool(updates["noise_suppression_enabled"])
            if "noise_suppression" in updates:
                channel.micro_noise_suppression = max(0, min(100, int(updates["noise_suppression"])))
            if "gate_enabled" in updates:
                channel.micro_gate_enabled = bool(updates["gate_enabled"])
            if "gate_threshold" in updates:
                channel.micro_gate_threshold = max(0, min(100, int(updates["gate_threshold"])))
            if "compressor_enabled" in updates:
                channel.micro_compressor_enabled = bool(updates["compressor_enabled"])
            if "compressor_amount" in updates:
                channel.micro_compressor_amount = max(0, min(100, int(updates["compressor_amount"])))
            if "deesser_enabled" in updates:
                channel.micro_deesser_enabled = bool(updates["deesser_enabled"])
            if "deesser_amount" in updates:
                channel.micro_deesser_amount = max(0, min(100, int(updates["deesser_amount"])))
            if "limiter_enabled" in updates:
                channel.micro_limiter_enabled = bool(updates["limiter_enabled"])
            if "limiter_ceiling_db" in updates:
                channel.micro_limiter_ceiling_db = max(-12.0, min(0.0, float(updates["limiter_ceiling_db"])))
        except (TypeError, ValueError):
            return False

        self.settings_store.save(self.settings)
        self._refresh_native_micro_hard_gate_runtime()
        self.status_changed.emit("MICRO processing updated")
        self.micro_processing_state_changed.emit()
        return True

    def micro_voice_fx_state(self) -> dict:
        channel = self._find_channel("micro")
        if channel is None:
            return {}
        order = ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
        raw = getattr(channel, "micro_voice_fx_active", [])
        active_set = {str(item or "").strip().lower() for item in raw} if isinstance(raw, list) else set()
        active = [key for key in order if key in active_set]
        if not active and bool(getattr(channel, "micro_voice_fx_enabled", False)):
            legacy = str(getattr(channel, "micro_voice_fx_preset", "radio") or "radio").strip().lower()
            if legacy not in order:
                legacy = "radio"
            active = [legacy]
        try:
            amount = int(getattr(channel, "micro_voice_fx_amount", 72))
        except Exception:
            amount = 72
        return {
            "active": active,
            "enabled": bool(active),
            "preset": active[0] if active else str(getattr(channel, "micro_voice_fx_preset", "radio") or "radio"),
            "amount": max(0, min(100, amount)),
        }

    def set_micro_voice_fx_state(self, updates: dict) -> bool:
        if not self.module_enabled("voice_fx"):
            return False
        channel = self._find_channel("micro")
        if channel is None or not isinstance(updates, dict):
            return False

        order = ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
        try:
            if "active" in updates:
                raw = updates.get("active")
                if not isinstance(raw, list):
                    return False
                requested = {str(item or "").strip().lower() for item in raw}
                active = [key for key in order if key in requested]
                # Keep mutually exclusive alternatives deterministic for hand-edited state.
                if "radio" in active and "telephone" in active:
                    active = [key for key in active if key != "telephone"]
                if "pitch_down" in active and "pitch_up" in active:
                    active = [key for key in active if key != "pitch_up"]
                room = [key for key in active if key in {"reverb", "church", "church_pa"}]
                if len(room) > 1:
                    keep = room[0]
                    active = [key for key in active if key not in {"reverb", "church", "church_pa"} or key == keep]
                channel.micro_voice_fx_active = active
                channel.micro_voice_fx_enabled = bool(active)
                if active:
                    channel.micro_voice_fx_preset = active[0]
            elif "enabled" in updates or "preset" in updates:
                # Backward-compatible IP13a writer path.
                preset = str(updates.get("preset", getattr(channel, "micro_voice_fx_preset", "radio")) or "radio").strip().lower()
                if preset not in order:
                    return False
                enabled = bool(updates.get("enabled", getattr(channel, "micro_voice_fx_enabled", False)))
                channel.micro_voice_fx_preset = preset
                channel.micro_voice_fx_active = [preset] if enabled else []
                channel.micro_voice_fx_enabled = enabled
            if "amount" in updates:
                channel.micro_voice_fx_amount = max(0, min(100, int(updates["amount"])))
        except (TypeError, ValueError):
            return False

        self.settings_store.save(self.settings)
        self._refresh_native_micro_hard_gate_runtime()
        state = self.micro_voice_fx_state()
        active = list(state.get("active", []))
        labels = {"distortion": "Distortion", "pitch_down": "Pitch ↓", "pitch_up": "Pitch ↑", "radio": "Radio Pilot", "telephone": "Telephone", "robot": "Robot 1", "reverb": "Reverb", "church": "Church", "church_pa": "Church PA"}
        status = " + ".join(labels.get(key, key) for key in active) if active else "Off"
        self.status_changed.emit(f"MICRO Voice FX → {status}")
        self.micro_voice_fx_state_changed.emit(self.micro_voice_fx_any_active())
        return True

    def voice_fx_user_presets(self) -> list[dict]:
        presets: list[dict] = []
        for _path, preset in self.preset_engine.list_for_target("vfx", runtime=True):
            presets.append(preset)
        return presets

    def micro_custom_fx_state(self) -> dict:
        channel = self._find_channel("micro")
        if channel is None:
            return {}
        preset = getattr(channel, "micro_custom_fx_preset", {})
        if not isinstance(preset, dict) or not preset:
            preset = self.preset_engine.new_preset("Custom Lab", "vfx")
        try:
            preset = self.preset_engine.runtime_chain(preset, "vfx")
        except PresetValidationError:
            preset = self.preset_engine.new_preset("Custom Lab", "vfx")
        return {
            "enabled": bool(getattr(channel, "micro_custom_fx_enabled", False)) and bool(preset.get("modules")),
            "preset": preset,
        }

    def micro_voice_fx_any_active(self) -> bool:
        native = self.micro_voice_fx_state()
        custom = self.micro_custom_fx_state()
        return bool(native.get("active")) or bool(custom.get("enabled"))

    def set_micro_custom_fx_state(self, updates: dict) -> bool:
        if not self.module_enabled("voice_fx"):
            return False
        channel = self._find_channel("micro")
        if channel is None or not isinstance(updates, dict):
            return False
        current = self.micro_custom_fx_state()
        preset = updates.get("preset", current.get("preset", {}))
        try:
            normalized = self.preset_engine.runtime_chain(preset, "vfx")
        except (PresetValidationError, TypeError, ValueError):
            return False
        enabled = bool(updates.get("enabled", current.get("enabled", False)))
        channel.micro_custom_fx_preset = normalized
        channel.micro_custom_fx_enabled = bool(enabled and normalized.get("modules"))
        self.settings_store.save(self.settings)
        self._refresh_native_micro_hard_gate_runtime()
        mode = "Auditioning" if channel.micro_custom_fx_enabled else "Bypassed"
        self.status_changed.emit(f"Preset Lab → {mode} · {normalized.get('name', 'Custom Lab')}")
        self.micro_custom_fx_state_changed.emit()
        self.micro_voice_fx_state_changed.emit(self.micro_voice_fx_any_active())
        return True

    def micro_hard_gate_enabled(self) -> bool:
        channel = self._find_channel("micro")
        return bool(getattr(channel, "hard_gate_enabled", False)) if channel is not None else False

    def micro_hard_gate_threshold(self) -> int:
        channel = self._find_channel("micro")
        if channel is None:
            return 12
        try:
            value = int(getattr(channel, "hard_gate_threshold", 12))
        except Exception:
            value = 12
        return max(0, min(100, value))

    def _sync_micro_hard_gate_audio_runtime(self) -> None:
        # Best path: native MICRO engine owns the physical mic audio path.
        # Python only writes state; it must not run the old pactl gate worker.
        if self._micro_hard_gate_uses_native_engine():
            self._stop_micro_hard_gate_monitor()
            self._refresh_native_micro_hard_gate_runtime()
            self._micro_gate_open = True
            return

        self._apply_micro_hard_gate_runtime()

    def set_micro_hard_gate_enabled(self, enabled: bool) -> bool:
        if not self.module_enabled("hard_gate"):
            return False
        channel = self._find_channel("micro")
        if channel is None:
            return False

        channel.hard_gate_enabled = bool(enabled)
        self.settings_store.save(self.settings)

        self._sync_micro_hard_gate_audio_runtime()
        self.status_changed.emit(f"MICRO input hard gate: {'ON' if bool(enabled) else 'OFF'}")
        return True


    def set_micro_hard_gate_threshold(self, value: int) -> bool:
        if not self.module_enabled("hard_gate"):
            return False
        channel = self._find_channel("micro")
        if channel is None:
            return False

        try:
            channel.hard_gate_threshold = max(0, min(100, int(value)))
        except Exception:
            channel.hard_gate_threshold = 12

        self.settings_store.save(self.settings)
        self._sync_micro_hard_gate_audio_runtime()
        self.status_changed.emit(f"MICRO input gate threshold: {int(channel.hard_gate_threshold)}%")
        return True


    def set_channel_volume(self, channel_key: str, value: int) -> bool:
        return self._apply_mixer_action(channel_key, "set-volume", volume=value)

    def set_channel_volume_live(self, channel_key: str, value: int) -> bool:
        """Update live volume state without overlay/status/save churn.

        Playback channels and MIC OUT get a lightweight native audio apply.
        MICRO stays visual/in-memory only during drag because that path can run
        pactl and micro-link logic.
        """
        key = self._normalize_channel_key(channel_key)
        channel = self._find_channel(key)
        if channel is None:
            return True

        try:
            channel.volume = max(0, min(100, int(value)))
        except Exception:
            return False

        # Keep the overlay alive during slider drag, but do not save, do not
        # emit full channel-state refreshes, and do not run heavy audio paths.
        try:
            self.overlay_message_requested.emit(self._overlay_text(channel, "volume"), False)
        except Exception:
            pass

        if key not in {"all", "game", "chat", "media", "more", "return-mic"}:
            return True

        # MIC OUT's muted slider is a SAFETY control: propagate it through
        # realtime and final volume apply even before the first unmute.
        if bool(channel.muted) and key != "return-mic":
            return True

        self._queue_realtime_volume_apply(channel.key)
        return True

    def set_channel_muted(self, channel_key: str, muted: bool) -> bool:
        return self._apply_mixer_action(channel_key, "set-mute", muted=muted)

    def _pactl_blocks(self, kind: str) -> list[dict[str, str]]:
        result = self._pactl("list", kind)
        if result.returncode != 0:
            return []

        blocks: list[dict[str, str]] = []
        current: dict[str, str] | None = None

        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            if re.match(r"^(Sink|Source) #\d+", line):
                if current:
                    blocks.append(current)
                current = {}
                continue

            if current is None:
                continue

            stripped = line.strip()
            if stripped.startswith("Name:"):
                current["name"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Description:"):
                current["description"] = stripped.split(":", 1)[1].strip()

        if current:
            blocks.append(current)

        return blocks

    def _dedupe_device_label(self, label: str, name: str, used: set[str]) -> str:
        clean = str(label or name or "Unknown device").strip()
        if clean not in used:
            used.add(clean)
            return clean

        short = str(name or "").strip()
        candidate = f"{clean} [{short}]" if short else clean
        index = 2
        while candidate in used:
            candidate = f"{clean} [{index}]"
            index += 1
        used.add(candidate)
        return candidate

    def available_output_targets(self) -> list[tuple[str, str]]:
        internal = {
            "all",
            "game",
            "chat",
            "media",
            "more",
            "retour",
            "micro_bus",
            "soundboard",
        }

        targets: list[tuple[str, str]] = []
        used: set[str] = set()

        for block in self._pactl_blocks("sinks"):
            name = str(block.get("name") or "").strip()
            if not name or name in internal:
                continue
            if name.endswith(".monitor"):
                continue

            description = str(block.get("description") or name).strip()
            lowered = f"{name} {description}".lower()
            if any(token in lowered for token in ["k-sound", "ksounds", "soundboard", "micro-bus", "retour-micro"]):
                continue

            label = self._dedupe_device_label(description, name, used)
            targets.append((label, name))

        return targets

    def available_input_targets(self) -> list[tuple[str, str]]:
        internal_sources = {"micro"}
        targets: list[tuple[str, str]] = []
        used: set[str] = set()

        for block in self._pactl_blocks("sources"):
            name = str(block.get("name") or "").strip()
            if not name or name in internal_sources or name.endswith(".monitor"):
                continue

            description = str(block.get("description") or name).strip()
            lowered = f"{name} {description}".lower()
            if any(token in lowered for token in ["k-sound-hub-soundboard", "soundboard", "retour"]):
                continue

            label = self._dedupe_device_label(description, name, used)
            targets.append((label, name))

        return targets

    def resolve_output_label(self, label: str) -> str:
        wanted = str(label or "").strip()
        dynamic = dict(self.available_output_targets())
        if wanted in dynamic:
            return dynamic[wanted]
        return ""

    def resolve_input_label(self, label: str) -> str:
        wanted = str(label or "").strip()
        dynamic = dict(self.available_input_targets())
        if wanted in dynamic:
            return dynamic[wanted]
        return ""

    def label_for_target(self, target: str, *, input_device: bool = False) -> str:
        wanted = str(target or "").strip()
        pairs = self.available_input_targets() if input_device else self.available_output_targets()
        for label, name in pairs:
            if name == wanted:
                return label

        return wanted

    def _list_pulse_modules_short(self) -> list[str]:
        result = self._pactl("list", "short", "modules")
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _unload_matching_modules(self, predicate) -> None:
        for line in self._list_pulse_modules_short():
            parts = line.split("\t", 2)
            if not parts:
                continue
            module_id = parts[0].strip()
            if not module_id.isdigit():
                continue
            try:
                if predicate(line):
                    self._pactl("unload-module", module_id)
            except Exception:
                pass

    def apply_soundboard_runtime_routes(self) -> None:
        output_key = self._soundboard_output_config()
        send_to_micro = self._soundboard_send_to_micro_config()
        output_sink = {
            "all": "all",
            "game": "game",
            "chat": "chat",
            "media": "media",
            "more": "more",
            "return-mic": "retour",
        }.get(output_key, "media")

        def is_old_soundboard_route(line: str) -> bool:
            lowered = line.lower()
            return (
                "module-loopback" in lowered
                and "source=soundboard.monitor" in lowered
                and any(f"sink={sink}" in lowered for sink in ["all", "game", "chat", "media", "more", "retour", "micro_bus"])
            )

        self._unload_matching_modules(is_old_soundboard_route)

        self._pactl(
            "load-module",
            "module-loopback",
            "source=soundboard.monitor",
            f"sink={output_sink}",
            "latency_msec=32",
            "source_dont_move=true",
            "sink_dont_move=true",
            "channels=2",
            "sink_input_properties=media.name=K-Sound-Hub-Soundboard-To-Output",
        )

        if self._micro_hard_gate_uses_native_engine():
            self._refresh_native_micro_hard_gate_runtime()
        elif send_to_micro:
            self._pactl(
                "load-module",
                "module-loopback",
                "source=soundboard.monitor",
                "sink=micro_bus",
                "latency_msec=32",
                "source_dont_move=true",
                "sink_dont_move=true",
                "channels=2",
                "sink_input_properties=media.name=K-Sound-Hub-Soundboard-To-Micro",
            )
            if bool(getattr(self, "_soundboard_pop_guard_active", False)):
                time.sleep(0.02)
                self._set_soundboard_route_streams_muted(True)

    def _reconcile_soundboard_mic_out_final(self) -> None:
        """Reconcile MIC OUT only after Glass has created/moved its pad route.

        This is startup/selector work, never per-pad playback work. MIC OUT's
        existing controller continues to own the final gain/mute and safety.
        """
        engine = getattr(self, "audio_engine", None)
        reconcile = getattr(engine, "_apply_return_mic", None)
        if callable(reconcile):
            reconcile(self._audio_runtime_settings())

    def apply_return_mic_runtime_route(self) -> None:
        # The audio engine already owns MIC OUT monitor loopbacks. Never load a
        # second, concurrently managed source=micro -> sink=retour here.
        self._adopt_return_mic_source_into_audio_engine()

    def _sync_soundboard_settings_links(self, output_key: str, send_to_micro: bool) -> None:
        output_key = self._normalize_soundboard_output_key(output_key)

        for key in ("all", "game", "chat", "media", "more", "return-mic", "micro"):
            channel = self._find_channel(key)
            if channel is None:
                continue

            linked = [
                str(item).strip().lower()
                for item in getattr(channel, "linked_channels", []) or []
                if str(item).strip()
            ]
            linked = [item for item in linked if item != "soundboard"]

            if key == output_key:
                linked.append("soundboard")
            if key == "micro" and send_to_micro:
                linked.append("soundboard")

            channel.linked_channels = linked

    def _normalize_soundboard_output_key(self, channel_key: str) -> str:
        raw = str(channel_key or "").strip().lower().replace("_", "-")
        aliases = {
            "mic out": "return-mic",
            "mic-out": "return-mic",
            "return mic": "return-mic",
            "return-mic": "return-mic",
            "retour": "return-mic",
        }
        raw = aliases.get(raw, raw)
        key = self._normalize_channel_key(raw)
        if key in {"all", "game", "chat", "media", "more", "return-mic"}:
            return key
        return "media"

    def _read_soundboard_document(self) -> dict:
        try:
            if SOUNDBOARD_PATH.is_file():
                loaded = json.loads(SOUNDBOARD_PATH.read_text(encoding="utf-8"))
            else:
                loaded = {}
            if isinstance(loaded, list):
                loaded = {"slots": loaded}
            if isinstance(loaded, dict):
                if not isinstance(loaded.get("slots"), list):
                    loaded["slots"] = []
                return loaded
        except Exception:
            pass
        return {"slots": []}

    def _write_soundboard_document(self, data: dict) -> None:
        try:
            if not isinstance(data, dict):
                data = {"slots": []}
            if not isinstance(data.get("slots"), list):
                data["slots"] = []
            SOUNDBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
            SOUNDBOARD_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as exc:
            self.status_changed.emit(f"Soundboard config save error — {exc}")

    def _read_settings_document(self) -> dict:
        try:
            path = CONFIG_DIR / "settings.json"
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return loaded
        except Exception:
            pass
        return {}

    def _write_settings_document(self, data: dict) -> None:
        try:
            # The normal SettingsStore.save(self.settings) also writes settings.json.
            # Mirror MIC OUT selections into that model before writing, otherwise
            # volume updates/autosave silently erase the selection while its
            # already-running PipeWire loopback keeps playing.
            settings = getattr(self, "settings", None)
            if settings is not None and "glass_return_mic_source" in data:
                settings.glass_return_mic_source = str(data.get("glass_return_mic_source") or "").strip()
                settings.glass_return_mic_micro_enabled = bool(data.get("glass_return_mic_micro_enabled", bool(settings.glass_return_mic_source)))
            path = CONFIG_DIR / "settings.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _pactl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["pactl", *[str(arg) for arg in args]],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _pulse_modules(self) -> list[str]:
        result = self._pactl("list", "short", "modules")
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _unload_modules_matching(self, predicate) -> None:
        for line in self._pulse_modules():
            parts = line.split("\t", 2)
            if not parts:
                continue
            module_id = parts[0].strip()
            if not module_id.isdigit():
                continue
            try:
                if predicate(line):
                    self._pactl("unload-module", module_id)
            except Exception:
                pass

    def _ensure_null_sink(self, sink_name: str, description: str) -> bool:
        name = str(sink_name or "").strip()
        label = str(description or name).strip()
        if not name:
            return False
        if self._sink_exists(name):
            return True

        self._pactl(
            "load-module",
            "module-null-sink",
            f"sink_name={name}",
            f"sink_properties=device.description={label}",
            "channels=2",
            "rate=48000",
        )
        time.sleep(0.04)
        return self._sink_exists(name)

    def _ensure_micro_endpoint(self) -> bool:
        if not self._ensure_null_sink("micro_bus", "🎤MICRO-BUS"):
            return False

        if not self._source_exists("micro"):
            self._pactl(
                "load-module",
                "module-remap-source",
                "master=micro_bus.monitor",
                "source_name=micro",
                "source_properties=device.description=🎤MICRO",
            )
            time.sleep(0.04)

        return self._source_exists("micro")

    def _ensure_glass_virtual_audio_buses(self) -> bool:
        # Glass is the frontend/runtime owner now. Do not rely on the old stable
        # UI or an external keepalive to create these logical channel sinks.
        wanted = (
            ("all", "🌍ALL"),
            ("game", "🎮GAME"),
            ("chat", "💬CHAT"),
            ("media", "🎵MEDIA"),
            ("more", "🔊MORE"),
            ("retour", "🎧MIC OUT"),
        )

        ok = True
        for sink_name, description in wanted:
            if not self._ensure_null_sink(sink_name, description):
                ok = False

        if not self._ensure_micro_endpoint():
            ok = False

        if not ok:
            self.status_changed.emit("Glass virtual audio buses incomplete — routing may be unsafe")
        return ok

    def _sink_exists(self, sink_name: str) -> bool:
        wanted = str(sink_name or "").strip()
        if not wanted:
            return False
        result = self._pactl("list", "short", "sinks")
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == wanted:
                return True
        return False

    def _source_exists(self, source_name: str) -> bool:
        wanted = str(source_name or "").strip()
        if not wanted:
            return False
        result = self._pactl("list", "short", "sources")
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == wanted:
                return True
        return False

    def _unload_soundboard_monitor_loopbacks(self) -> None:
        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "source=soundboard.monitor" in line.lower()
            )
        )

    def _unload_rigid_soundboard_output_loopbacks(self) -> None:
        # Old route instances were loaded with sink_dont_move=true, which makes
        # pactl move-sink-input fail and forces unload/reload on every output
        # switch. Remove only that old Soundboard-To-Output route; keep MICRO.
        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "source=soundboard.monitor" in line.lower()
                and "sink=micro_bus" not in line.lower()
                and "sink_dont_move=true" in line.lower()
            )
        )

    def _ensure_soundboard_bus(self) -> bool:
        # Glass plays every pad into the private soundboard sink, then routes
        # soundboard.monitor to MEDIA / MIC OUT / MICRO. If the private bus is
        # missing, a loopback that says source=soundboard.monitor can become a
        # stale or wrong live route. Repair the bus before accepting any route.
        if self._sink_exists("soundboard") and self._source_exists("soundboard.monitor"):
            return True

        self._unload_soundboard_monitor_loopbacks()

        if self._sink_exists("soundboard") and not self._source_exists("soundboard.monitor"):
            self._unload_modules_matching(
                lambda line: (
                    "module-null-sink" in line.lower()
                    and "sink_name=soundboard" in line.lower()
                )
            )

        if not self._sink_exists("soundboard"):
            self._pactl(
                "load-module",
                "module-null-sink",
                "sink_name=soundboard",
                "sink_properties=device.description=🎛SOUNDBOARD media.name=K-Sound-Hub-Soundboard-Bus",
                "channels=2",
                "rate=48000",
            )
            time.sleep(0.05)
            if bool(getattr(self, "_soundboard_pop_guard_active", False)):
                self._set_soundboard_bus_muted(True)

        ok = self._sink_exists("soundboard") and self._source_exists("soundboard.monitor")
        if not ok:
            self.status_changed.emit("Soundboard bus missing — cannot route Soundboard output safely")
        return ok

    def _soundboard_output_config(self) -> str:
        data = self._read_soundboard_document()

        raw = str(data.get("output_channel") or "").strip()
        if raw:
            return self._normalize_soundboard_output_key(raw)

        if bool(data.get("monitor_to_mic_out")):
            return "return-mic"

        return "media"

    def _soundboard_send_to_micro_config(self) -> bool:
        data = self._read_soundboard_document()
        value = data.get("send_to_micro")
        if isinstance(value, bool):
            return value
        return False

    def _return_mic_micro_enabled(self) -> bool:
        data = self._read_settings_document()
        if isinstance(data.get("glass_return_mic_micro_enabled"), bool):
            return bool(data.get("glass_return_mic_micro_enabled"))

        for channel in data.get("channels", []) if isinstance(data.get("channels"), list) else []:
            if isinstance(channel, dict) and channel.get("key") == "return-mic":
                linked = {str(item).strip().lower() for item in channel.get("linked_channels", []) or []}
                return bool({"micro", "micro-final"} & linked)
        return False

    def _save_return_mic_micro_enabled(self, enabled: bool) -> None:
        data = self._read_settings_document()
        data["glass_return_mic_micro_enabled"] = bool(enabled)

        # Do not use linked_channels for these Glass-only runtime routes anymore.
        for channel in data.get("channels", []) if isinstance(data.get("channels"), list) else []:
            if isinstance(channel, dict) and channel.get("key") == "return-mic":
                linked = [
                    str(item).strip().lower()
                    for item in channel.get("linked_channels", []) or []
                    if str(item).strip().lower() not in {"micro", "micro-final", "soundboard"}
                ]
                channel["linked_channels"] = linked

        self._write_settings_document(data)

    def _sink_inputs_by_media_name(self, media_name: str) -> list[str]:
        result = self._pactl("list", "sink-inputs")
        if result.returncode != 0:
            return []

        wanted = str(media_name or "").strip()
        ids: list[str] = []
        current_id: str | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_id, current_lines
            if current_id is not None:
                block = "\n".join(current_lines)
                if f'media.name = "{wanted}"' in block or f"media.name = {wanted}" in block:
                    ids.append(current_id)
            current_id = None
            current_lines = []

        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            m = re.match(r"Sink Input #(\d+)", line)
            if m:
                flush()
                current_id = m.group(1)
                current_lines = [line]
            elif current_id is not None:
                current_lines.append(line)

        flush()
        return ids

    def _sink_input_sink_names_by_media_name(self, media_name: str) -> list[str]:
        wanted = str(media_name or "").strip()
        if not wanted:
            return []

        sink_names_by_index: dict[str, str] = {}
        sinks = self._pactl("list", "short", "sinks")
        if sinks.returncode == 0:
            for line in sinks.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit():
                    sink_names_by_index[parts[0]] = parts[1]

        result = self._pactl("list", "sink-inputs")
        if result.returncode != 0:
            return []

        names: list[str] = []
        current_sink_index: str | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_sink_index, current_lines
            if current_sink_index is None:
                current_lines = []
                return
            block = "\n".join(current_lines)
            if f'media.name = "{wanted}"' in block or f"media.name = {wanted}" in block:
                names.append(sink_names_by_index.get(current_sink_index, current_sink_index))
            current_sink_index = None
            current_lines = []

        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            if re.match(r"Sink Input #(\d+)", line):
                flush()
                current_sink_index = None
                current_lines = [line]
                continue

            if not current_lines:
                continue

            stripped = line.strip()
            if stripped.startswith("Sink:"):
                current_sink_index = stripped.split(":", 1)[1].strip()
            current_lines.append(line)

        flush()
        return names

    def _set_soundboard_bus_muted(self, muted: bool) -> None:
        if not self._sink_exists("soundboard"):
            return
        self._pactl("set-sink-mute", "soundboard", "1" if muted else "0")

    def _set_soundboard_route_streams_muted(self, muted: bool) -> None:
        flag = "1" if muted else "0"
        for media_name in (
            "K-Sound-Hub-Soundboard-To-Output",
            "K-Sound-Hub-Soundboard-To-Micro",
        ):
            for stream_id in self._sink_inputs_by_media_name(media_name):
                self._pactl("set-sink-input-mute", stream_id, flag)

    def _unmute_soundboard_output_streams(self, *, force: bool = False) -> None:
        if bool(getattr(self, "_soundboard_pop_guard_active", False)) and not force:
            return
        for stream_id in self._sink_inputs_by_media_name("K-Sound-Hub-Soundboard-To-Output"):
            self._pactl("set-sink-input-mute", stream_id, "0")
            self._pactl("set-sink-input-volume", stream_id, "100%")

    def _move_soundboard_output_streams(self, sink: str) -> bool:
        target = str(sink or "").strip()
        if not target or not self._sink_exists(target):
            return False

        stream_ids = self._sink_inputs_by_media_name("K-Sound-Hub-Soundboard-To-Output")
        if not stream_ids:
            return False

        moved = False
        for stream_id in stream_ids:
            result = self._pactl("move-sink-input", stream_id, target)
            if result.returncode == 0:
                moved = True

        if moved:
            self._unmute_soundboard_output_streams()
        return moved

    def _soundboard_output_sink_for_key(self, output_key: str) -> str:
        return {
            "all": "all",
            "game": "game",
            "chat": "chat",
            "media": "media",
            "more": "more",
            "return-mic": "retour",
        }.get(self._normalize_soundboard_output_key(output_key), "media")

    def _soundboard_output_loopback_ok(self, output_key: str) -> bool:
        if not self._sink_exists("soundboard") or not self._source_exists("soundboard.monitor"):
            return False

        sink = self._soundboard_output_sink_for_key(output_key)
        if not self._sink_exists(sink):
            return False

        matching_ids: list[str] = []
        for line in self._pulse_modules():
            parts = line.split("\t", 2)
            module_id = parts[0].strip() if parts else ""
            low = line.lower()
            if not ("module-loopback" in low and "source=soundboard.monitor" in low):
                continue
            if "sink=micro_bus" in low:
                continue
            if f"sink={sink}" in low:
                if module_id.isdigit():
                    matching_ids.append(module_id)
            elif module_id.isdigit():
                self._pactl("unload-module", module_id)

        for module_id in matching_ids[:-1]:
            self._pactl("unload-module", module_id)

        if len(matching_ids) != 1:
            return False

        actual_sinks = self._sink_input_sink_names_by_media_name("K-Sound-Hub-Soundboard-To-Output")
        if actual_sinks and (sink not in actual_sinks or any(name != sink for name in actual_sinks)):
            # PipeWire-pulse can keep a loopback module with args sink=<logical>
            # while its stream has fallen back to the default physical sink if
            # the logical sink was missing at load time. Treat that as stale.
            for module_id in matching_ids:
                self._pactl("unload-module", module_id)
            return False

        return True

    def _soundboard_micro_loopback_ok(self) -> bool:
        if not self._sink_exists("soundboard") or not self._source_exists("soundboard.monitor"):
            return False
        if not self._sink_exists("micro_bus"):
            return False

        matching_ids: list[str] = []
        for line in self._pulse_modules():
            parts = line.split("\t", 2)
            module_id = parts[0].strip() if parts else ""
            low = line.lower()
            if (
                "module-loopback" in low
                and "source=soundboard.monitor" in low
                and "sink=micro_bus" in low
                and module_id.isdigit()
            ):
                matching_ids.append(module_id)

        for module_id in matching_ids[:-1]:
            self._pactl("unload-module", module_id)

        if len(matching_ids) != 1:
            return False

        actual_sinks = self._sink_input_sink_names_by_media_name("K-Sound-Hub-Soundboard-To-Micro")
        if actual_sinks and ("micro_bus" not in actual_sinks or any(name != "micro_bus" for name in actual_sinks)):
            for module_id in matching_ids:
                self._pactl("unload-module", module_id)
            return False

        return True

    def _load_soundboard_output_loopback(self, output_key: str) -> None:
        self._ensure_glass_virtual_audio_buses()
        if not self._ensure_soundboard_bus():
            return

        sink = self._soundboard_output_sink_for_key(output_key)
        if not self._sink_exists(sink):
            self._unload_modules_matching(
                lambda line: (
                    "module-loopback" in line.lower()
                    and "source=soundboard.monitor" in line.lower()
                    and "sink=micro_bus" not in line.lower()
                )
            )
            self.status_changed.emit(f"Soundboard output sink missing — {sink}")
            return

        self._unload_rigid_soundboard_output_loopbacks()

        # Prefer moving the existing Soundboard-To-Output loopback stream. This
        # avoids destroying/recreating the loopback on every output switch, which
        # produces audible pops/crackles even when no pad is playing.
        if self._move_soundboard_output_streams(sink):
            return

        # Critical for pads latency: if the correct listening route already
        # exists, do NOT unload/reload it before each sound.
        if self._soundboard_output_loopback_ok(output_key):
            self._unmute_soundboard_output_streams()
            return

        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "source=soundboard.monitor" in line.lower()
                and "sink=micro_bus" not in line.lower()
            )
        )

        self._pactl(
            "load-module",
            "module-loopback",
            "source=soundboard.monitor",
            f"sink={sink}",
            "latency_msec=32",
            "source_dont_move=true",
            "channels=2",
            "sink_input_properties=media.name=K-Sound-Hub-Soundboard-To-Output",
        )

        time.sleep(0.04)
        if bool(getattr(self, "_soundboard_pop_guard_active", False)):
            self._set_soundboard_route_streams_muted(True)
        else:
            self._unmute_soundboard_output_streams()

    def _unload_soundboard_micro_loopback(self) -> None:
        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "source=soundboard.monitor" in line.lower()
                and "sink=micro_bus" in line.lower()
            )
        )

    def _load_soundboard_micro_loopback(self, enabled: bool) -> None:
        if self._micro_hard_gate_uses_native_engine():
            if enabled:
                self._ensure_glass_virtual_audio_buses()
                if not self._ensure_soundboard_bus():
                    return

            self._refresh_native_micro_hard_gate_runtime()
            self._unload_soundboard_micro_loopback()
            return

        if enabled:
            self._ensure_glass_virtual_audio_buses()
            if not self._ensure_soundboard_bus():
                return

        exists = self._soundboard_micro_loopback_ok()

        # Critical for pads latency: do not reload the MICRO route on every pad.
        if enabled and exists:
            return
        if not enabled and not exists:
            return

        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "source=soundboard.monitor" in line.lower()
                and "sink=micro_bus" in line.lower()
            )
        )

        if enabled:
            self._pactl(
                "load-module",
                "module-loopback",
                "source=soundboard.monitor",
                "sink=micro_bus",
                "latency_msec=32",
                "source_dont_move=true",
                "sink_dont_move=true",
                "channels=2",
                "sink_input_properties=media.name=K-Sound-Hub-Soundboard-To-Micro",
            )
            if bool(getattr(self, "_soundboard_pop_guard_active", False)):
                time.sleep(0.02)
                self._set_soundboard_route_streams_muted(True)

    def _return_mic_source_config(self) -> str:
        # AppSettings is the authoritative in-memory state for this setting;
        # SettingsStore saves it alongside mixer volume, mute and other options.
        settings = getattr(self, "settings", None)
        if settings is not None and hasattr(settings, "glass_return_mic_source"):
            return str(settings.glass_return_mic_source or "").strip()
        data = self._read_settings_document()
        # Off must be really Off. Do not infer from old glass_return_mic_micro_enabled.
        return str(data.get("glass_return_mic_source") or "").strip()

    def processed_micro_source_label(self) -> str:
        # Same PipeWire device.description used by Discord for the exported
        # source_name=micro, without exposing micro as a physical capture input.
        for block in self._pactl_blocks("sources"):
            if str(block.get("name") or "").strip() != "micro":
                continue
            description = str(block.get("description") or "").strip()
            if description:
                physical = {label for label, _source in self.available_input_targets()}
                return f"{description} (K-Sounds)" if description in physical else description
        return "MICRO (K-Sounds)"

    def return_mic_source_label(self) -> str:
        source = self._return_mic_source_config()
        if not source:
            return "Off"
        if source == "micro":
            return self.processed_micro_source_label()
        return self.label_for_target(source, input_device=True) or "Off"

    def _return_mic_volume_state(self) -> tuple[int, bool]:
        channel = self._find_channel("return-mic")
        if channel is None:
            return 100, False

        try:
            volume = int(getattr(channel, "volume", 100))
        except Exception:
            volume = 100

        volume = max(0, min(180, volume))
        muted = bool(getattr(channel, "muted", False))
        return volume, muted

    def _apply_return_mic_visible_volume(self) -> None:
        # MIC OUT level is applied by the final playback engine. Reapplying the
        # same slider to its capture loopback *and* its final output doubles the
        # gain and risks a loud transient. Kept as a compatibility no-op.
        return

    def _unload_return_mic_source_loopbacks(self) -> None:
        # Remove ONLY the named legacy IP14n/n2 Glass monitor route, not the
        # V2 engine's monitor and never Soundboard -> retour.
        self._unload_modules_matching(
            lambda line: (
                "module-loopback" in line.lower()
                and "sink=retour" in line.lower()
                and "k-sound-hub-return-mic-micro" in line.lower()
            )
        )

    def _adopt_return_mic_source_into_audio_engine(self) -> None:
        """Give the existing V2 MIC OUT engine exclusive control of this route.

        This function does not load a Pulse loopback. Both mono physical sources
        and the stereo processed MICRO follow the engine's existing channel
        negotiation and its muted route-change protection.
        """
        channel = self._find_channel("return-mic")
        if channel is None:
            return
        selected = self._return_mic_source_config()
        old = list(getattr(channel, "linked_channels", []) or [])
        # The selection represents one source. Strip older selection aliases;
        # retain unrelated links and the independently owned Soundboard route.
        linked = [item for item in old if not (
            str(item).strip().lower() in {"micro", "micro-final"}
            or str(item).strip().lower().startswith("source:")
            or str(item).strip() == selected
        )]
        if selected:
            linked.append("micro-final" if selected == "micro" else f"source:{selected}")
        channel.linked_channels = linked
        self._unload_return_mic_source_loopbacks()
        # The native playback engine performs MIC OUT capture, final output
        # level and mute reconciliation; do not multiply those gains in Glass.
        self.audio_engine.apply_channel(self._audio_runtime_settings(), "return-mic")
        if linked != old:
            self._save_timer.start()

    def _load_return_mic_micro_loopback(self, enabled: bool) -> None:
        # Compatibility entry point for former GUI/runtime callers.
        # The source preference is already kept in AppSettings.
        self._adopt_return_mic_source_into_audio_engine()

    def _select_return_mic_source(self, target: str, display: str) -> bool:
        """Select the MIC OUT transport without ever changing its mix controls.

        The old selector clamped channel.volume to 12 on every selection and
        saved that clamp, which desynchronized the slider and actual playback.
        Route transitions are guarded *inside* the native transport instead.
        """
        target = str(target or "").strip()
        channel = self._find_channel("return-mic")
        if channel is None:
            return False
        previous = self._return_mic_source_config()
        # Persist selection through the sole SettingsStore model.  Do not
        # write a stale settings.json copy that can overwrite other controls.
        self.settings.glass_return_mic_source = target
        self.settings.glass_return_mic_micro_enabled = bool(target)
        if target != previous:
            store = getattr(self, "settings_store", None)
            if store is not None:
                # Save once from the authoritative in-memory model. This also
                # persists the CURRENT slider value, never a transient cap.
                store.save(self.settings)
            else:
                # Small test/legacy harnesses without SettingsStore.
                data = self._read_settings_document()
                data["glass_return_mic_source"] = target
                data["glass_return_mic_micro_enabled"] = bool(target)
                self._write_settings_document(data)
        self._adopt_return_mic_source_into_audio_engine()
        self._save_timer.start()
        # A repeated selection is a genuine route recovery request.
        self.status_changed.emit(
            f"MIC OUT source → {display}" if target != previous
            else f"MIC OUT source refreshed → {display}"
        )
        notifier = getattr(self, "channel_state_changed", None)
        if notifier is not None:
            notifier.emit("return-mic", int(channel.volume), bool(channel.muted))
        return True

    def set_return_mic_source_label(self, label: str) -> bool:
        selected = str(label or "").strip()
        if not selected or selected.lower() == "off":
            target = ""
        elif selected.upper() in {"MICRO", "MICRO (K-SOUNDS)"} or selected == self.processed_micro_source_label():
            target = "micro"
        else:
            target = self.resolve_input_label(selected)
            if not target:
                return False
        return self._select_return_mic_source(target, selected)

    def return_mic_route_state(self) -> dict[str, bool]:
        enabled = bool(self._return_mic_source_config())
        return {"micro": enabled, "micro-final": enabled}

    def set_return_mic_source_state(self, source_key: str, enabled: bool) -> bool:
        source = str(source_key or "").strip()
        if source in {"micro", "micro-final"}:
            source = "micro"
        if not enabled:
            source = ""
        elif source != "micro" and source not in {
            name for _label, name in self.available_input_targets()
        }:
            return False
        return self._select_return_mic_source(source, source or "Off")

    def _sync_legacy_linked_channels_off(self) -> None:
        # Prevent the older channel engine from re-creating mixed routes.
        settings = self._read_settings_document()
        channels = settings.get("channels", [])
        if not isinstance(channels, list):
            return

        for channel in channels:
            if not isinstance(channel, dict):
                continue
            linked = [
                str(item).strip().lower()
                for item in channel.get("linked_channels", []) or []
                if str(item).strip().lower() not in {"soundboard", "micro", "micro-final"}
            ]
            channel["linked_channels"] = linked

        self._write_settings_document(settings)

    def _ksh_mic_physical_source_name(self) -> str:
        for line in self._pulse_modules():
            lowered = line.lower()
            if (
                "module-loopback" not in lowered
                or "sink=micro_bus" not in lowered
                or "ksh_mic_physical" not in lowered
            ):
                continue
            for part in line.split():
                if part.startswith("source="):
                    return part.split("=", 1)[1].strip()
        return ""

    def _set_ksh_mic_physical_gate_open(self, open_gate: bool) -> None:
        channel = self._find_channel("micro")
        user_muted = bool(getattr(channel, "muted", False)) if channel is not None else False

        # Avoid Pulse/PipeWire mute edges for the hard gate. Manual testing
        # showed sink-input volume 0/100 creates a softer transition than
        # set-sink-input-mute. Add a very short volume ramp to de-click the
        # remaining 0↔100 edge without changing the gate logic.
        effective_open = bool(open_gate) and not user_muted

        previous_open = bool(getattr(self, "_micro_gate_open", False))
        if effective_open == previous_open:
            return

        ramp = (25, 55, 80, 100) if effective_open else (70, 35, 10, 0)
        sink_input_ids = tuple(self._sink_inputs_by_media_name("KSH_MIC_PHYSICAL"))

        for sink_input_id in sink_input_ids:
            # Keep the stream unmuted and gate by volume only.
            self._pactl("set-sink-input-mute", str(sink_input_id), "0")

        for volume in ramp:
            for sink_input_id in sink_input_ids:
                self._pactl("set-sink-input-volume", str(sink_input_id), f"{volume}%")
            time.sleep(0.004)

        self._micro_gate_open = effective_open

    def _micro_gate_effective_threshold(self) -> float:
        value = self.micro_hard_gate_threshold()
        return 0.0015 + ((float(value) / 100.0) ** 2.0) * 0.18

    def _micro_channel_volume_scalar(self) -> float:
        channel = self._find_channel("micro")
        if channel is None:
            return 1.0
        try:
            value = int(getattr(channel, "volume", 100))
        except Exception:
            value = 100
        return max(0.0, min(1.5, float(value) / 100.0))

    def _stop_micro_hard_gate_monitor(self) -> None:
        self._micro_gate_stop.set()

        process = self._micro_gate_process
        self._micro_gate_process = None
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass

        thread = self._micro_gate_thread
        self._micro_gate_thread = None
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=0.35)
            except Exception:
                pass

        self._micro_gate_source_name = ""
        self._micro_gate_stop = threading.Event()

    def _micro_hard_gate_uses_native_engine(self) -> bool:
        engine = getattr(self, "audio_engine", None)
        enabled = getattr(engine, "_native_micro_enabled", None)
        try:
            return bool(enabled()) if callable(enabled) else False
        except Exception:
            return False

    def _refresh_native_micro_hard_gate_runtime(self) -> None:
        engine = getattr(self, "audio_engine", None)
        refresh = getattr(engine, "refresh_native_micro_state", None)
        if callable(refresh):
            try:
                refresh(self._audio_runtime_settings())
                return
            except Exception:
                pass

        writer = getattr(engine, "_write_native_micro_state", None)
        ensure = getattr(engine, "_ensure_native_micro_engine", None)
        try:
            if callable(writer):
                writer(self._audio_runtime_settings())
            if callable(ensure):
                ensure()
        except Exception:
            pass

    def _apply_micro_hard_gate_runtime(self) -> None:
        # IP13b unification: the native MICRO engine is the only runtime Hard
        # Gate implementation. The old parec/pactl worker is never started.
        self._stop_micro_hard_gate_monitor()
        if self._micro_hard_gate_uses_native_engine():
            self._refresh_native_micro_hard_gate_runtime()
            self._micro_gate_open = True
            return

        # Native MICRO unavailable: fail open rather than silently enabling the
        # legacy second gate path. TUNE keeps the saved settings for next start.
        self._set_ksh_mic_physical_gate_open(True)
        self._micro_gate_open = True

    def _micro_hard_gate_worker(self, source_name: str, stop_event: threading.Event) -> None:
        import struct

        cmd = [
            "parec",
            f"--device={source_name}",
            "--format=s16le",
            "--rate=48000",
            "--channels=1",
            "--latency-msec=25",
        ]

        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._micro_gate_process = process
            self._micro_gate_last_above = 0.0
            self._micro_gate_last_apply = 0.0
            self._set_ksh_mic_physical_gate_open(False)

            while not stop_event.is_set():
                if process.stdout is None:
                    break

                data = process.stdout.read(2048)
                if not data:
                    break

                sample_count = len(data) // 2
                if sample_count <= 0:
                    continue

                try:
                    samples = struct.unpack("<" + ("h" * sample_count), data[: sample_count * 2])
                    raw_peak = max(abs(sample) for sample in samples) / 32768.0
                except Exception:
                    raw_peak = 0.0

                # Gate follows the user-facing MICRO input volume.
                # Only KSH_MIC_PHYSICAL is volume-gated: soundboard and
                # MICRO Injection routes into micro_bus are not gated.
                peak = raw_peak * self._micro_channel_volume_scalar()

                now = time.monotonic()
                threshold = self._micro_gate_effective_threshold()
                open_threshold = threshold
                close_threshold = max(0.0005, threshold * 0.58)

                if peak >= open_threshold:
                    self._micro_gate_last_above = now
                    if not self._micro_gate_open and (now - self._micro_gate_last_apply) >= 0.030:
                        self._set_ksh_mic_physical_gate_open(True)
                        self._micro_gate_last_apply = now
                elif self._micro_gate_open and peak < close_threshold:
                    if (now - self._micro_gate_last_above) >= 0.145 and (now - self._micro_gate_last_apply) >= 0.060:
                        self._set_ksh_mic_physical_gate_open(False)
                        self._micro_gate_last_apply = now

        except Exception as exc:
            try:
                self.status_changed.emit(f"MICRO hard gate monitor stopped — {exc}")
            except Exception:
                pass
        finally:
            if process is not None:
                try:
                    process.terminate()
                except Exception:
                    pass
            if self._micro_gate_process is process:
                self._micro_gate_process = None

    def micro_injection_channels(self) -> set[str]:
        allowed = {"all", "game", "chat", "media", "more"}
        raw = getattr(self.settings, "glass_micro_injection_channels", None)
        if raw is None:
            data = self._read_settings_document()
            raw = data.get("glass_micro_injection_channels", [])
        if not isinstance(raw, list):
            raw = []
        return {str(item).strip().lower() for item in raw if str(item).strip().lower() in allowed}

    def micro_injection_volume(self) -> int:
        raw = getattr(self.settings, "glass_micro_injection_volume", None)
        if raw is None:
            data = self._read_settings_document()
            raw = data.get("glass_micro_injection_volume", 125)
        try:
            value = int(raw)
        except Exception:
            value = 125
        return max(0, min(200, value))

    def set_micro_injection_volume(self, volume: int) -> bool:
        try:
            value = int(volume)
        except Exception:
            return False
        value = max(0, min(200, value))

        self.settings.glass_micro_injection_volume = value
        self.settings_store.save(self.settings)

        self.apply_micro_injection_runtime_routes()
        self.status_changed.emit(f"MICRO injection volume: {value}%")
        return True

    def _micro_inject_sink_input_ids(self, channel_key: str) -> list[str]:
        key = str(channel_key or "").strip().lower()
        if not key:
            return []
        return self._sink_inputs_matching_tokens([f"k-sound-hub-micro-inject-{key}"])

    def _sink_input_volume_percent(self, sink_input_id: str) -> int | None:
        wanted = str(sink_input_id).strip()
        if not wanted:
            return None

        result = self._pactl("list", "sink-inputs")
        if result.returncode != 0:
            return None

        current = ""
        in_target = False
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("Sink Input #"):
                current = line.split("#", 1)[1].strip()
                in_target = current == wanted
                continue

            if not in_target:
                continue

            if line.startswith("Volume:"):
                import re
                match = re.search(r"/\s*(\d+)%\s*/", line)
                if match:
                    try:
                        return int(match.group(1))
                    except Exception:
                        return None
        return None

    def _set_micro_inject_live_volume(self, channel_key: str, volume_percent: int, *, smooth: bool = True) -> None:
        target = max(0, min(200, int(volume_percent)))

        for sink_input_id in self._micro_inject_sink_input_ids(channel_key):
            if not smooth:
                self._pactl("set-sink-input-volume", str(sink_input_id), f"{target}%")
                continue

            current = self._sink_input_volume_percent(str(sink_input_id))
            if current is None:
                current = 0 if target > 0 else target

            delta = target - current
            if abs(delta) <= 2:
                # Idempotent refresh: do not re-apply the same Pulse/PipeWire
                # sink-input volume. Some loopback paths can pop even when the
                # requested value is identical to the current value.
                continue

            steps = max(6, min(24, int(abs(delta) / 7) + 1))
            for index in range(1, steps + 1):
                t = index / steps
                # Smoothstep easing avoids a hard first/last jump.
                eased = t * t * (3.0 - 2.0 * t)
                value = int(round(current + (delta * eased)))
                self._pactl("set-sink-input-volume", str(sink_input_id), f"{value}%")
                time.sleep(0.012)

    def _silence_micro_inject_before_unload(self, channel_key: str) -> None:
        self._set_micro_inject_live_volume(channel_key, 0, smooth=True)
        time.sleep(0.08)

    def _unload_micro_inject_loopbacks(self) -> None:
        allowed = {"all", "game", "chat", "media", "more"}
        modules: list[tuple[str, str]] = []

        for line in self._pulse_modules():
            lowered = line.lower()
            parts = line.split("\t", 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue

            if "module-loopback" not in lowered or "sink=micro_bus" not in lowered:
                continue

            matched_key = ""
            for key in allowed:
                if f"k-sound-hub-micro-inject-{key}" in lowered:
                    matched_key = key
                    break

            if not matched_key:
                continue

            modules.append((matched_key, parts[0]))

        for key, module_id in modules:
            # Native MICRO engine now owns these sends. Force the old Pulse
            # loopback to silence before unloading to avoid a one-shot thump.
            self._set_micro_inject_live_volume(key, 0, smooth=False)
            self._pactl("unload-module", module_id)

    def set_micro_injection_channel_state(self, channel_key: str, enabled: bool) -> bool:
        key = str(channel_key or "").strip().lower()
        allowed = {"all", "game", "chat", "media", "more"}
        if key not in allowed:
            return False

        current = self.micro_injection_channels()
        if enabled:
            current.add(key)
        else:
            current.discard(key)

        self.settings.glass_micro_injection_channels = sorted(current)
        self.settings_store.save(self.settings)

        # Immediate and stable: only add/remove the changed channel loopback state.
        self.apply_micro_injection_runtime_routes()
        self.status_changed.emit(f"MICRO injection {key.upper()}: {bool(enabled)}")
        return True

    def apply_micro_injection_runtime_routes(self) -> None:
        if self._micro_hard_gate_uses_native_engine():
            self._refresh_native_micro_hard_gate_runtime()
            self._unload_micro_inject_loopbacks()
            return

        enabled = self.micro_injection_channels()
        allowed = {"all", "game", "chat", "media", "more"}
        volume = self.micro_injection_volume()

        existing: dict[str, str] = {}
        duplicate_ids: list[tuple[str, str]] = []

        for line in self._pulse_modules():
            lowered = line.lower()
            parts = line.split("\t", 2)
            if not parts or not parts[0].isdigit():
                continue

            if "module-loopback" not in lowered or "sink=micro_bus" not in lowered:
                continue

            matched_key = ""
            for key in allowed:
                if f"k-sound-hub-micro-inject-{key}" in lowered or f"source={key}.monitor" in lowered:
                    matched_key = key
                    break

            if not matched_key:
                continue

            if matched_key not in existing:
                existing[matched_key] = parts[0]
            else:
                duplicate_ids.append((matched_key, parts[0]))

        # Remove only duplicates/stale clones. Normal OFF state keeps the one
        # valid loopback alive at 0%, avoiding load/unload pops on every toggle.
        for key, module_id in duplicate_ids:
            self._silence_micro_inject_before_unload(key)
            self._pactl("unload-module", module_id)

        for key in sorted(allowed):
            if key not in existing:
                self._pactl(
                    "load-module",
                    "module-loopback",
                    f"source={key}.monitor",
                    "sink=micro_bus",
                    "latency_msec=40",
                    "source_dont_move=true",
                    "sink_dont_move=true",
                    "channels=2",
                    f"sink_input_properties=media.name=K-Sound-Hub-Micro-Inject-{key}",
                )
                time.sleep(0.04)

            target_volume = volume if key in enabled else 0
            self._set_micro_inject_live_volume(key, target_volume)

    def apply_glass_runtime_routes(self) -> None:
        self._sync_legacy_linked_channels_off()
        self._ensure_glass_virtual_audio_buses()
        self._load_soundboard_output_loopback(self._soundboard_output_config())
        self._load_soundboard_micro_loopback(self._soundboard_send_to_micro_config())
        # Route ownership is entirely in the V2 audio engine; adopt the
        # selected source without introducing a second Glass loopback.
        if self._return_mic_source_config():
            self._adopt_return_mic_source_into_audio_engine()
        else:
            self._unload_return_mic_source_loopbacks()
        self._apply_micro_hard_gate_runtime()
        self.apply_micro_injection_runtime_routes()
        self.normalize_channel_playback_routes()
        if self._soundboard_output_config() == "return-mic" and not self._return_mic_source_config():
            self._reconcile_soundboard_mic_out_final()

    def _soundboard_route_state(self) -> dict[str, bool]:
        data = self._read_soundboard_document()
        output = self._normalize_soundboard_output_key(str(data.get("output_channel") or "media"))
        return {
            "send_to_micro": bool(data.get("send_to_micro", False)),
            "monitor_to_mic_out": output == "return-mic",
        }

    def _set_soundboard_route_state(self, route_key: str, enabled: bool) -> bool:
        route = str(route_key or "").strip().lower()

        if route == "monitor_to_mic_out":
            return self.set_soundboard_output_channel("return-mic" if enabled else "media")

        if route != "send_to_micro":
            return False

        data = self._read_soundboard_document()
        data["send_to_micro"] = bool(enabled)
        for slot in data.get("slots", []):
            if isinstance(slot, dict):
                slot["send_to_micro"] = bool(enabled)
        self._write_soundboard_document(data)

        self._sync_legacy_linked_channels_off()
        self._load_soundboard_micro_loopback(bool(enabled))
        self.status_changed.emit(f"Soundboard MICRO: {bool(enabled)}")
        return True

    def _sink_inputs_matching_tokens(self, tokens: list[str]) -> list[str]:
        result = self._pactl("list", "sink-inputs")
        if result.returncode != 0:
            return []

        wanted = [str(token or "").lower() for token in tokens if str(token or "").strip()]
        ids: list[str] = []
        current_id: str | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_id, current_lines
            if current_id is None:
                return
            block = "\n".join(current_lines).lower()
            if any(token in block for token in wanted):
                ids.append(current_id)
            current_id = None
            current_lines = []

        for raw in result.stdout.splitlines():
            line = raw.rstrip()
            match = re.match(r"Sink Input #(\d+)", line)
            if match:
                flush()
                current_id = match.group(1)
                current_lines = [line]
            elif current_id is not None:
                current_lines.append(line)

        flush()
        return ids

    def _move_channel_playback_to_target(self, channel_key: str, target_sink: str) -> None:
        key = str(channel_key or "").strip().lower()
        target = str(target_sink or "").strip()
        if not key or key == "micro":
            return

        label = {
            "all": "ALL",
            "game": "GAME",
            "chat": "CHAT",
            "media": "MEDIA",
            "more": "MORE",
            "return-mic": "RETURN-MIC",
        }.get(key, key.upper())

        tokens = [
            f"ksh_{key}_eq.playback",
            f"K-Sound Hub {label} EQ",
            f"K-Sound Hub {label.replace('-', ' ')} EQ",
        ]

        stream_ids = self._sink_inputs_matching_tokens(tokens)
        if not stream_ids:
            return

        numeric_ids = sorted({int(stream_id) for stream_id in stream_ids})
        keep = str(numeric_ids[-1])

        for stream_id in numeric_ids[:-1]:
            self._pactl("kill-sink-input", str(stream_id))

        if target:
            self._pactl("move-sink-input", keep, target)

    def _persist_channel_primary_target(self, channel_key: str, target: str) -> None:
        data = self._read_settings_document()
        channels = data.get("channels", [])
        if not isinstance(channels, list):
            return

        for channel in channels:
            if isinstance(channel, dict) and channel.get("key") == channel_key:
                channel["primary_target"] = target
                break

        self._write_settings_document(data)

    def normalize_channel_playback_routes(self) -> None:
        for key in ("all", "game", "chat", "media", "more", "return-mic"):
            channel = self._find_channel(key)
            if channel is None:
                continue
            target = str(getattr(channel, "primary_target", "") or "").strip()
            self._move_channel_playback_to_target(key, target)

    def set_channel_primary_target(self, channel_key: str, target_sink: str) -> bool:
        key = str(channel_key or "").strip()
        target = str(target_sink or "").strip()

        channel = self._find_channel(key)
        if channel is None or not target:
            return False

        if key == "micro":
            valid_targets = {name for _label, name in self.available_input_targets()}
            if target not in valid_targets:
                self.status_changed.emit(f"Unsafe MICRO input rejected — {target}")
                return False

        channel.primary_target = target

        try:
            self.audio_engine.apply_channel(self._audio_runtime_settings(), channel.key)
        except Exception as exc:
            self.status_changed.emit(f"Target route warning — {exc}")

        self._persist_channel_primary_target(channel.key, target)

        if channel.key != "micro":
            self._move_channel_playback_to_target(channel.key, target)

        if channel.key in {"micro", "return-mic"}:
            self._apply_return_mic_visible_volume()
            QTimer.singleShot(120, self._apply_return_mic_visible_volume)

        self._save_timer.start()

        if channel.key == "micro":
            label = self.label_for_target(target, input_device=True)
        else:
            label = self.label_for_target(target, input_device=False)

        self.status_changed.emit(f"{GLASS_CHANNEL_OVERLAY_META.get(channel.key, ('', channel.key.upper()))[1]} device → {label}")
        return True

    def channel_primary_target(self, channel_key: str) -> str:
        channel = self._find_channel(channel_key)
        if channel is None:
            return ""
        return str(getattr(channel, "primary_target", "") or "").strip()

    def favorite_device_targets(self, *, input_device: bool = False) -> list[str]:
        attr = "favorite_input_targets" if input_device else "favorite_output_targets"
        raw = getattr(self.settings, attr, [])
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            target = str(item or "").strip()
            if not target or target in seen:
                continue
            seen.add(target)
            result.append(target)
        return result

    def is_favorite_device_target(self, target: str, *, input_device: bool = False) -> bool:
        wanted = str(target or "").strip()
        return bool(wanted and wanted in self.favorite_device_targets(input_device=input_device))

    def set_favorite_device_target(self, target: str, favorite: bool, *, input_device: bool = False) -> bool:
        wanted = str(target or "").strip()
        if not wanted:
            return False
        attr = "favorite_input_targets" if input_device else "favorite_output_targets"
        current = self.favorite_device_targets(input_device=input_device)
        if favorite:
            if wanted not in current:
                current.append(wanted)
        else:
            current = [item for item in current if item != wanted]
        setattr(self.settings, attr, current)
        self._save_timer.start()
        label = self.label_for_target(wanted, input_device=input_device) or wanted
        self.status_changed.emit(f"{'Favorite' if favorite else 'Unfavorited'} device → {label}")
        return True

    def favorite_micro_eq_presets(self) -> list[str]:
        raw = getattr(self.settings, "favorite_micro_eq_presets", [])
        names, _selected = self.eq_profile_names("micro")
        valid = set(names)
        result: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            name = str(item or "").strip()
            if name and name in valid and name not in result:
                result.append(name)
        return result

    def is_favorite_micro_eq_preset(self, name: str) -> bool:
        return str(name or "").strip() in self.favorite_micro_eq_presets()

    def set_favorite_micro_eq_preset(self, name: str, favorite: bool) -> bool:
        wanted = str(name or "").strip()
        names, _selected = self.eq_profile_names("micro")
        if not wanted or wanted not in names:
            return False
        current = self.favorite_micro_eq_presets()
        if favorite and wanted not in current:
            current.append(wanted)
        elif not favorite:
            current = [item for item in current if item != wanted]
        self.settings.favorite_micro_eq_presets = current
        self._save_timer.start()
        self.status_changed.emit(f"{'Favorite' if favorite else 'Unfavorited'} MICRO preset → {wanted}")
        return True

    def favorite_output_eq_presets(self) -> list[str]:
        names, _selected = self.eq_profile_names("all")
        valid = set(names)
        raw = getattr(self.settings, "favorite_output_eq_presets", [])
        result: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            name = str(item or "").strip()
            if name and name in valid and name not in result:
                result.append(name)
        return result

    def is_favorite_output_eq_preset(self, name: str) -> bool:
        return str(name or "").strip() in self.favorite_output_eq_presets()

    def set_favorite_output_eq_preset(self, name: str, favorite: bool) -> bool:
        wanted = str(name or "").strip()
        names, _selected = self.eq_profile_names("all")
        if not wanted or wanted not in names:
            return False
        current = self.favorite_output_eq_presets()
        if favorite and wanted not in current:
            current.append(wanted)
        elif not favorite:
            current = [item for item in current if item != wanted]
        self.settings.favorite_output_eq_presets = current
        self._save_timer.start()
        self.status_changed.emit(f"{'Favorite' if favorite else 'Unfavorited'} output EQ preset → {wanted}")
        return True

    def soundboard_pad_shortcut_state(self, slot_key: str) -> tuple[str, bool]:
        # MainWindow owns the central ShortcutRegistry. Detached/Main Pads UI
        # reaches that single source of truth through this lightweight resolver.
        resolver = getattr(self, "_soundboard_shortcut_state_resolver", None)
        if callable(resolver):
            try:
                trigger, enabled = resolver(str(slot_key or "").strip())
                clean = str(trigger or "").strip()
                return clean, bool(enabled and clean)
            except Exception:
                pass

        # Compatibility fallback for startup/tests before MainWindow attaches
        # the resolver. Legacy Soundboard data already has a `shortcut` field.
        slot = self.soundboard_service.slot_for_key(slot_key)
        if not isinstance(slot, dict):
            return "", False
        trigger = str(slot.get("shortcut") or "").strip()
        return trigger, bool(trigger)

    def request_soundboard_pad_shortcut_update(
        self,
        slot_key: str,
        *,
        trigger=None,
        enabled=None,
    ) -> bool:
        wanted = str(slot_key or "").strip()
        if not wanted:
            return False
        self.soundboard_shortcut_edit_requested.emit(wanted, trigger, enabled)
        return True

    def sync_soundboard_pad_shortcut_state(
        self,
        slot_key: str,
        trigger: str,
        enabled: bool,
    ) -> bool:
        wanted = str(slot_key or "").strip()
        if not wanted:
            return False
        data = self.soundboard_service.read_document()
        slots = data.get("slots", []) if isinstance(data, dict) else []
        changed = False
        found = False
        clean_trigger = str(trigger or "").strip()
        clean_enabled = bool(enabled and clean_trigger)
        # Keep the legacy per-slot field as a compatibility mirror only. A
        # disabled central binding must not remain live in the old local
        # Soundboard QShortcut implementation, so the mirror is blank while OFF.
        legacy_trigger = clean_trigger if clean_enabled else ""
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            if str(slot.get("id") or "").strip() != wanted:
                continue
            found = True
            if str(slot.get("shortcut") or "").strip() != legacy_trigger:
                slot["shortcut"] = legacy_trigger
                changed = True
            # Do not add a second persistence field here. Enabled/disabled lives
            # in AppSettings.shortcut_bindings, the same source as Mixer/TUNE.
            if "shortcut_enabled" in slot:
                slot.pop("shortcut_enabled", None)
                changed = True
            break
        if not found:
            return False
        if changed:
            self.soundboard_service.write_document(data)
        self.soundboard_shortcut_state_changed.emit(wanted, clean_trigger, clean_enabled)
        return True

    def play_soundboard_slot_shortcut(self, slot_key: str) -> bool:
        if not self.module_enabled("pads"):
            return False
        wanted = str(slot_key or "").strip()
        slot = self.soundboard_service.slot_for_key(wanted)
        if not isinstance(slot, dict):
            self.status_changed.emit("Soundboard shortcut: pad no longer exists")
            return False

        ok = bool(self.soundboard_service.play_slot(slot))
        label = str(slot.get("label") or wanted or "Pad").strip() or "Pad"
        if ok:
            # Playback stays on SoundboardService's non-GUI process path. No
            # heavy route rebuild is introduced by the shortcut.
            self.overlay_message_requested.emit(f"🎛 {label}", False)
            QTimer.singleShot(120, self._unmute_soundboard_output_streams)
            self.status_changed.emit(f"Soundboard shortcut → {label}")
        else:
            self.status_changed.emit(f"Soundboard shortcut failed → {label}")
        return ok

    def set_soundboard_micro_enabled(self, enabled: bool) -> bool:
        data = self._read_soundboard_document()
        data["send_to_micro"] = bool(enabled)
        self._write_soundboard_document(data)
        self._load_soundboard_micro_loopback(bool(enabled))
        self.status_changed.emit(f"Soundboard MICRO → {bool(enabled)}")
        return True

    def soundboard_output_channel(self) -> str:
        return self._soundboard_output_config()

    def set_soundboard_output_channel(self, channel_key: str) -> bool:
        wanted = self._normalize_soundboard_output_key(channel_key)
        previous = self._soundboard_output_config()

        data = self._read_soundboard_document()
        data["output_channel"] = wanted
        data["monitor_to_mic_out"] = wanted == "return-mic"
        self._write_soundboard_document(data)

        self._sync_legacy_linked_channels_off()
        self._load_soundboard_output_loopback(wanted)
        if wanted == "return-mic" or previous == "return-mic":
            self._reconcile_soundboard_mic_out_final()

        label = SOUNDBOARD_LOGICAL_LABEL_BY_KEY.get(wanted, wanted.upper())
        self.status_changed.emit(f"Soundboard output → {label}")
        return True


    def _handle_soundboard_command(self, payload: dict, command: str) -> bool:
        command = str(command or "").strip().lower()
        if command not in {
            "soundboard",
            "soundboard-play",
            "soundboard_play",
            "soundboard-stop-all",
            "soundboard_stop_all",
            "soundboard-set-global-volume",
            "soundboard_set_global_volume",
            "soundboard-move-slot",
            "soundboard_move_slot",
            "soundboard-move-to-folder",
            "soundboard-reorder-slots",
            "soundboard_reorder_slots",
        }:
            return False

        module_enabled = getattr(self, "module_enabled", lambda _module_id: True)
        if not module_enabled("pads"):
            self.status_changed.emit("Soundboard command ignored — Pads module disabled")
            return True

        service = self.soundboard_service

        if command in {"soundboard-stop-all", "soundboard_stop_all"}:
            service.stop_all()
            self.overlay_message_requested.emit("🎛 Soundboard stop all", False)
            self.status_changed.emit("Android remote: stop all")
            return True

        if command in {"soundboard-set-global-volume", "soundboard_set_global_volume"}:
            ok = service.set_global_volume(payload.get("volume"))
            if ok:
                self.soundboard_changed.emit()
            self.status_changed.emit(
                f"Android remote: global volume {payload.get('volume')} {'OK' if ok else 'failed'}"
            )
            return True

        if command in {"soundboard-move-slot", "soundboard_move_slot"}:
            slot = str(payload.get("slot") or payload.get("id") or payload.get("label") or "")
            direction = str(payload.get("direction") or "")
            ok = service.move_slot_by_key(slot, direction)
            if ok:
                self.soundboard_changed.emit()
            self.status_changed.emit(f"Android remote: move {slot} {direction} {'OK' if ok else 'failed'}")
            return True

        if command == "soundboard-move-to-folder":
            from .soundboard_library import remote_folder_move
            try:
                updated, moved, folder = remote_folder_move(
                    service.read_document(), payload.get("ids"), payload.get("folder"),
                    expected_folder=payload.get("expected_folder"))
                if moved:
                    service.write_document(updated)
                    self.soundboard_changed.emit()
                self.status_changed.emit(f"Android remote: sounds moved to {folder or 'Main'}")
            except (ValueError, OSError) as exc:
                self.status_changed.emit(f"Android remote: {exc}")
            return True

        if command in {"soundboard-reorder-slots", "soundboard_reorder_slots"}:
            order = payload.get("order")
            ok = service.reorder_slots_by_ids(order if isinstance(order, list) else [])
            if ok:
                self.soundboard_changed.emit()
            self.status_changed.emit(f"Android remote: reorder {'OK' if ok else 'failed'}")
            return True

        if command in {"soundboard", "soundboard-play", "soundboard_play"}:
            slot = str(payload.get("slot") or payload.get("id") or payload.get("label") or "")
            ok = service.play_slot_by_key(slot)
            if ok:
                self.overlay_message_requested.emit(f"🎛 Soundboard {slot}", False)
            self.status_changed.emit(f"Android remote: play {slot} {'OK' if ok else 'failed'}")
            return True

        return False


    def _handle_voice_fx_remote_command(self, payload: dict, command: str) -> bool:
        command = str(command or "").strip().lower()
        if command not in {"voice-fx-toggle", "voice_fx_toggle"}:
            return False
        module_enabled = getattr(self, "module_enabled", lambda _module_id: True)
        if not module_enabled("voice_fx"):
            self.status_changed.emit("Voice FX command ignored — module disabled")
            return True

        effect = str(payload.get("effect") or "").strip().lower()
        order = ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
        if effect not in order:
            self.status_changed.emit(f"Android remote: unknown Voice FX {effect or '?'}")
            return True

        state = self.micro_voice_fx_state()
        current = [
            key for key in order
            if key in {str(item or "").strip().lower() for item in state.get("active", [])}
        ]
        raw_enabled = payload.get("enabled", None)
        if raw_enabled is None:
            enabled = effect not in current
        elif isinstance(raw_enabled, bool):
            enabled = raw_enabled
        else:
            enabled = str(raw_enabled or "").strip().lower() in {"1", "true", "yes", "on"}

        active = [key for key in current if key != effect]
        if enabled:
            # Keep the same exclusivity rules as the desktop VFX surface, but
            # make the newly requested Remote effect win instead of being
            # discarded by canonical ordering in set_micro_voice_fx_state().
            if effect == "radio":
                active = [key for key in active if key != "telephone"]
            elif effect == "telephone":
                active = [key for key in active if key != "radio"]
            elif effect in {"pitch_down", "pitch_up"}:
                active = [key for key in active if key not in {"pitch_down", "pitch_up"}]
            elif effect in {"reverb", "church", "church_pa"}:
                active = [key for key in active if key not in {"reverb", "church", "church_pa"}]
            active.append(effect)

        requested = {key for key in active}
        canonical = [key for key in order if key in requested]
        ok = self.set_micro_voice_fx_state({"active": canonical})
        label = {
            "distortion": "Distortion",
            "pitch_down": "Pitch ↓",
            "pitch_up": "Pitch ↑",
            "radio": "Radio Pilot",
            "telephone": "Telephone",
            "robot": "Robot 1",
            "reverb": "Reverb",
            "church": "Church",
            "church_pa": "Church PA",
        }.get(effect, effect)
        self.status_changed.emit(
            f"Android remote: Voice FX {label} {'ON' if enabled else 'OFF'} {'OK' if ok else 'failed'}"
        )
        return True


    def handle_ipc_message(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return

        command = str(payload.get("command", "")).strip().lower()
        if command == "restore":
            self.restore_requested.emit()
            return

        if command in {"restart-ui-preserve-audio", "restart_ui_preserve_audio"}:
            self.ui_restart_requested.emit()
            return

        if command == "external-module-ui-action":
            request_id = payload.get("request_id")
            mid, sid, cid, action = (
                payload.get("module"),
                payload.get("surface"),
                payload.get("control"),
                payload.get("module_action"),
            )

            matches = [
                control
                for surface in self.extension_registry.active_surfaces(
                    "remote"
                )
                if (
                    surface.module_id == mid
                    and surface.surface_id == sid
                )
                for control in surface.controls
                if (
                    control.control_id == cid
                    and control.action == action
                    and control.kind != "label"
                )
            ]

            if len(matches) == 1:
                control = matches[0]

                descriptor = {
                    "type": control.kind,
                    "min": control.minimum,
                    "max": control.maximum,
                    "options": [
                        {"value": value}
                        for _, value in control.options
                    ],
                }

                if remote_valid_value(
                    descriptor,
                    payload.get("value"),
                ):
                    if self.invoke_module_ui_action(mid, sid, cid, action, payload["value"], request_id=request_id):
                        return

            if isinstance(request_id, str):
                receipt_finish(
                    CONFIG_DIR / RECEIPT_DIR,
                    request_id,
                    False,
                    "Module or value unavailable",
                )
            return

        if command and self._handle_voice_fx_remote_command(payload, command):
            return

        if command and self._handle_soundboard_command(payload, command):
            return

        channel_key = self._normalize_channel_key(payload.get("channel", ""))
        action = str(payload.get("action", "")).strip().lower()
        if not channel_key or not action:
            return

        self._apply_mixer_action(
            channel_key,
            action,
            volume=payload.get("volume"),
            muted=payload.get("muted", payload.get("checked", None)),
        )

    def _set_mixer_shortcut_volume_target(
        self,
        channel_key: str,
        volume: int,
        *,
        show_overlay: bool = False,
        final: bool = False,
    ) -> bool:
        """Shortcut-only volume path: local model/audio target without UI feedback loops."""
        channel = self._find_channel(channel_key)
        if channel is None:
            return False
        # MIC OUT is armed in Mute on every NEW monitor-source selection.
        # Its volume MUST remain adjustable while muted so the user can
        # prepare a safe low level before manually enabling headphones.
        # Other muted channels preserve their historical shortcut behavior.
        if bool(channel.muted) and channel.key != "return-mic":
            return True

        try:
            target = max(0, min(100, int(volume)))
        except Exception:
            return False

        channel.volume = target
        if show_overlay:
            self.overlay_message_requested.emit(self._overlay_text(channel, "volume"), False)

        self._queue_realtime_volume_apply(channel.key, final=final)
        self._save_timer.start()

        if final:
            # Visual/model reconciliation is immediate, but the heavy full
            # audio reconciliation is performed by the worker above.
            self.channel_state_changed.emit(channel.key, int(channel.volume), bool(channel.muted))
        return True

    def _apply_mixer_action(self, channel_key: str, action: str, *, volume=None, muted=None) -> bool:
        channel = self._find_channel(channel_key)
        if channel is None:
            return False

        action = str(action or "").strip().lower()
        hint = "generic"
        changed = False

        if action in {"set-mute", "set_mute"}:
            if isinstance(muted, str):
                channel.muted = muted.strip().lower() in {"1", "true", "yes", "on", "muted"}
            else:
                channel.muted = bool(muted)
            hint = "mute"
            changed = True

        elif action in {"volup", "voldown", "set-volume"} and channel.muted and channel.key != "return-mic":
            # Other muted channels retain stable shortcut behavior. MIC OUT
            # must be adjustable while muted to avoid a first audible jump.
            self.channel_state_changed.emit(channel.key, int(channel.volume), bool(channel.muted))
            self.overlay_message_requested.emit(self._overlay_text(channel, "mute"), True)
            return True

        elif action == "volup":
            channel.volume = min(100, int(channel.volume) + 5)
            hint = "volume"
            changed = True

        elif action == "voldown":
            channel.volume = max(0, int(channel.volume) - 5)
            hint = "volume"
            changed = True

        elif action == "mute":
            channel.muted = not bool(channel.muted)
            hint = "mute"
            changed = True

        elif action == "set-volume":
            try:
                channel.volume = max(0, min(100, int(volume)))
            except Exception:
                return False
            hint = "volume"
            changed = True

        if not changed:
            return False

        # UI first: shortcuts feel instant, audio apply is coalesced right after.
        self.channel_state_changed.emit(channel.key, int(channel.volume), bool(channel.muted))
        if hint in {"volume", "mute"}:
            self.overlay_message_requested.emit(
                self._overlay_text(channel, hint),
                bool(hint == "mute" and channel.muted),
            )

        if hint == "volume":
            self._queue_realtime_volume_apply(channel.key)
        else:
            self._pending_apply_keys.add(channel.key)
            if not self._apply_timer.isActive():
                self._apply_timer.start()

        self._save_timer.start()
        return True

    def _queue_realtime_volume_apply(self, channel_key: str, *, final: bool = False) -> None:
        key = self._normalize_channel_key(channel_key)
        if not key:
            return
        condition = self._realtime_volume_condition
        with condition:
            self._realtime_volume_pending.add(key)
            if final:
                self._realtime_volume_final_pending.add(key)
            condition.notify()

    def _realtime_volume_worker(self) -> None:
        """Apply only the newest channel targets, entirely off the GUI thread."""
        condition = self._realtime_volume_condition
        min_period = 1.0 / 60.0
        while True:
            with condition:
                while not self._realtime_volume_pending and not self._realtime_volume_stopping:
                    condition.wait()
                if self._realtime_volume_stopping:
                    return
                keys = set(self._realtime_volume_pending)
                final_keys = set(self._realtime_volume_final_pending)
                self._realtime_volume_pending.clear()
                self._realtime_volume_final_pending.difference_update(keys)

            started = time.monotonic()
            realtime_apply = getattr(self.audio_engine, "apply_channel_volume_realtime", None)
            fast_apply = getattr(self.audio_engine, "apply_channel_volume_fast", None)
            for key in keys:
                try:
                    if callable(realtime_apply):
                        realtime_apply(self.settings, key)
                    elif callable(fast_apply):
                        fast_apply(self.settings, key)
                    else:
                        self.audio_engine.apply_channel(self._audio_runtime_settings(), key)
                    if key in final_keys:
                        # Full endpoint/route reconciliation also stays off the
                        # GUI thread. The visible slider has already reached
                        # its final target and must never wait for this work.
                        self.audio_engine.apply_channel(self._audio_runtime_settings(), key)
                except Exception as exc:
                    try:
                        self._realtime_volume_error.emit(f"Volume apply error on {key} — {exc}")
                    except Exception:
                        pass

            # Cap backend work at 60 Hz. New requests remain coalesced in the
            # pending set while this worker is sleeping; the GUI never waits.
            remaining = min_period - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

    def _stop_realtime_volume_worker(self) -> None:
        condition = getattr(self, "_realtime_volume_condition", None)
        if condition is None:
            return
        with condition:
            self._realtime_volume_stopping = True
            self._realtime_volume_pending.clear()
            self._realtime_volume_final_pending.clear()
            condition.notify_all()
        thread = getattr(self, "_realtime_volume_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.35)

    def _flush_pending_volume_fast_apply(self) -> None:
        """Compatibility hook: realtime work is now owned by the worker."""
        return

    def _flush_pending_channel_apply(self) -> None:
        keys = set(self._pending_apply_keys)
        self._pending_apply_keys.clear()

        for key in keys:
            try:
                self.audio_engine.apply_channel(self._audio_runtime_settings(), key)

                if key in {"micro", "return-mic"}:
                    self._apply_return_mic_visible_volume()
                    QTimer.singleShot(120, self._apply_return_mic_visible_volume)
            except Exception as exc:
                self.status_changed.emit(f"Channel apply error on {key} — {exc}")

    def _autosave(self) -> None:
        try:
            self.settings_store.save(self.settings)
        except Exception as exc:
            self.status_changed.emit(f"Settings save error — {exc}")


CHANNELS = [
    ("ALL", str(CHANNEL_ICON_PATHS["all"]), [], 76, "all"),
    ("GAME", str(CHANNEL_ICON_PATHS["game"]), [], 72, "game"),
    ("CHAT", str(CHANNEL_ICON_PATHS["chat"]), [], 70, "chat"),
    ("MEDIA", str(CHANNEL_ICON_PATHS["media"]), [], 64, "media"),
    ("MORE", str(CHANNEL_ICON_PATHS["more"]), [], 58, "more"),
    ("MICRO", str(CHANNEL_ICON_PATHS["micro"]), [], 84, "micro"),
    ("MIC OUT", str(CHANNEL_ICON_PATHS["return-mic"]), [], 52, "return-mic"),
]

MIXER_SHORTCUT_ACTIONS = (
    ("mute", "Mute"),
    ("voldown", "Volume Down"),
    ("volup", "Volume Up"),
    ("device_next", "Next favorite device"),
)
CHANNEL_SHORTCUT_LABELS = {channel_key: name for name, _icon, _devices, _value, channel_key in CHANNELS}


def mixer_shortcut_id(channel_key: str, action: str) -> str:
    return f"mixer.{str(channel_key).strip()}.{str(action).strip()}"


APP_ROUTE_CHANNELS = [
    ("ALL", "all"),
    ("GAME", "game"),
    ("CHAT", "chat"),
    ("MEDIA", "media"),
    ("MORE", "more"),
]
APP_ROUTE_LABEL_BY_KEY = {key: label for label, key in APP_ROUTE_CHANNELS}
APP_ROUTE_KEY_BY_LABEL = {label: key for label, key in APP_ROUTE_CHANNELS}
APP_ROUTE_KEYS = {key for _label, key in APP_ROUTE_CHANNELS}

SOUNDBOARD_LOGICAL_OUTPUTS = [
    ("ALL", "all"),
    ("GAME", "game"),
    ("CHAT", "chat"),
    ("MEDIA", "media"),
    ("MORE", "more"),
    ("MIC OUT", "return-mic"),
]
SOUNDBOARD_LOGICAL_LABEL_BY_KEY = {key: label for label, key in SOUNDBOARD_LOGICAL_OUTPUTS}
SOUNDBOARD_LOGICAL_KEY_BY_LABEL = {label: key for label, key in SOUNDBOARD_LOGICAL_OUTPUTS}


STYLE = """
QWidget {
    color: #edf5ff;
    background: transparent;
    font-family: "Inter", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 12px;
}

QMainWindow {
    background: #02050a;
}

QWidget#root,
QWidget#foreground {
    background: transparent;
}

QLabel#backgroundImage {
    background: #02050a;
}

QFrame#backgroundTint {
    background: rgba(0, 0, 0, 130);
    border: none;
}

QFrame#backgroundWash {
    background: rgba(90, 130, 255, 24);
    border: none;
}

QFrame#titleBar {
    background: rgba(0, 0, 0, 126);
    border: none;
}

QFrame#contentFrame {
    background: rgba(0, 0, 0, 52);
    border: none;
}

QFrame#navRail,
QFrame#channelCard,
QFrame#drawer {
    background: rgba(0, 0, 0, 178);
    border: none;
    border-radius: 15px;
}

QFrame#channelCard:hover {
    background: rgba(5, 13, 22, 194);
    border: none;
}

QFrame#channelCard[muted="true"] {
    background: rgba(82, 18, 24, 190);
    border: none;
}

QFrame#channelCard[muted="true"]:hover {
    background: rgba(104, 22, 31, 205);
    border: none;
}

QPushButton#windowButton {
    min-width: 30px;
    max-width: 30px;
    min-height: 24px;
    max-height: 24px;
    padding: 0px;
    border-radius: 6px;
    background: transparent;
    border: none;
    color: rgba(226, 242, 255, 190);
}

QPushButton#windowButton:hover {
    background: rgba(30, 56, 84, 150);
    border: none;
}

QPushButton#closeButton {
    min-width: 30px;
    max-width: 30px;
    min-height: 24px;
    max-height: 24px;
    padding: 0px;
    border-radius: 6px;
    background: transparent;
    border: none;
    color: rgba(255, 220, 228, 205);
}

QPushButton#closeButton:hover {
    background: rgba(190, 38, 58, 170);
    border: none;
}

QPushButton#navButton {
    background: transparent;
    border: none;
    border-radius: 12px;
    padding: 7px 2px;
    color: rgba(218, 236, 250, 170);
    font-size: 10px;
}

QPushButton#navButton:checked {
    background: rgba(20, 106, 176, 82);
    border: none;
    color: #f2fbff;
}

QPushButton#navButton:hover {
    background: rgba(18, 40, 62, 115);
    border: none;
}

QLabel#appIcon {
    min-width: 22px;
    min-height: 22px;
    max-width: 22px;
    max-height: 22px;
    border-radius: 0px;
    background: transparent;
    border: none;
    color: #dff7ff;
    font-size: 12px;
    font-weight: 900;
}

QLabel#titleText {
    color: rgba(222, 238, 250, 190);
    font-size: 11px;
    font-weight: 700;
}
QLabel#versionText {
    color: rgba(120, 205, 245, 145);
    font-size: 9px;
    font-weight: 800;
    padding-left: 1px;
}

QLabel#muted {
    color: rgba(205, 224, 242, 150);
    font-size: 10px;
}

QLabel#channelIcon {
    min-width: 38px;
    min-height: 38px;
    max-width: 38px;
    max-height: 38px;
    border-radius: 0px;
    border: none;
    background: transparent;
    color: #86dcff;
    font-size: 21px;
    font-weight: 900;
}

QLabel#channelName {
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 1.7px;
}

QLabel#volumeValue {
    color: rgba(225, 242, 255, 212);
    font-size: 11px;
    font-weight: 850;
}

QLabel#sectionTitle {
    color: #e8f5ff;
    font-size: 13px;
    font-weight: 900;
    letter-spacing: 1.2px;
}

QPushButton {
    background: rgba(8, 17, 28, 128);
    border: none;
    border-radius: 10px;
    padding: 5px 9px;
}

QPushButton:hover {
    background: rgba(14, 32, 50, 150);
    border: none;
}

QPushButton#muteButton {
    padding: 5px 5px;
    font-size: 10px;
}

QPushButton#muteButton:checked {
    background: rgba(160, 42, 52, 170);
    color: #ffecef;
    border: none;
}

QPushButton#primaryButton {
    background: rgba(35, 142, 226, 112);
    border: none;
}

QPushButton#demoButton {
    background: rgba(35, 142, 226, 105);
    border: none;
    border-radius: 10px;
    padding: 7px 10px;
    font-weight: 750;
}

QPushButton#toggleSwitch {
    min-width: 54px;
    max-width: 54px;
    min-height: 25px;
    max-height: 25px;
    border-radius: 13px;
    padding: 0px;
    background: rgba(25, 35, 48, 180);
    border: none;
    color: rgba(210, 225, 240, 180);
    font-size: 10px;
    font-weight: 800;
}

QPushButton#toggleSwitch:checked {
    background: rgba(45, 165, 240, 175);
    color: #f2fbff;
}

QFrame#padsTopBar {
    background: rgba(0, 0, 0, 80);
    border: none;
    border-radius: 12px;
}

QPushButton#padTopButton {
    background: rgba(20, 38, 55, 145);
    border: none;
    border-radius: 10px;
    padding: 7px 9px;
    font-weight: 760;
}

QPushButton#padTopButton:hover {
    background: rgba(35, 80, 115, 155);
    border: none;
}

QPushButton#padTopButton:checked {
    background: rgba(45, 150, 225, 160);
    border: none;
}

QPushButton#padAddButton {
    min-width: 32px;
    max-width: 32px;
    min-height: 30px;
    max-height: 30px;
    background: rgba(55, 165, 238, 150);
    border: none;
    border-radius: 15px;
    padding: 0px;
    font-size: 17px;
    font-weight: 900;
}

QFrame#soundPadCard {
    background: rgba(0, 0, 0, 150);
    border: none;
    border-radius: 14px;
}

QFrame#soundPadCard:hover {
    background: rgba(8, 24, 38, 176);
    border: none;
}

QFrame#soundPadCard[edit="true"] {
    background: rgba(11, 31, 47, 188);
    border: none;
}

QLabel#soundPadIcon {
    min-width: 34px;
    min-height: 30px;
    max-height: 30px;
    background: transparent;
    border: none;
    font-size: 22px;
}

QLabel#soundPadName {
    color: rgba(240, 248, 255, 225);
    font-size: 11px;
    font-weight: 780;
}

QLabel#soundPadMeta {
    color: rgba(205, 224, 242, 140);
    font-size: 9px;
}

QLabel#soundPadTrim {
    color: rgba(235, 248, 255, 220);
    font-size: 9px;
    font-weight: 780;
}

QCheckBox#soundboardAutoLevelToggle {
    color: rgba(235, 248, 255, 235);
    background: rgba(10, 22, 34, 185);
    border: 1px solid rgba(100, 190, 255, 75);
    border-radius: 10px;
    padding: 5px 10px;
    font-size: 10px;
    font-weight: 780;
}
QCheckBox#soundboardAutoLevelToggle:hover {
    background: rgba(34, 82, 120, 195);
    border: 1px solid rgba(120, 210, 255, 130);
}
QCheckBox#soundboardAutoLevelToggle::indicator {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    margin-right: 7px;
}
QCheckBox#soundboardAutoLevelToggle::indicator:unchecked {
    background: rgba(0, 0, 0, 210);
    border: 1px solid rgba(235, 248, 255, 120);
}
QCheckBox#soundboardAutoLevelToggle::indicator:checked {
    background: rgba(78, 198, 255, 235);
    border: 1px solid rgba(210, 250, 255, 240);
}

QFrame#soundPadActions {
    background: rgba(0, 0, 0, 90);
    border: none;
    border-radius: 10px;
}

QPushButton#padIconButton {
    min-width: 26px;
    max-width: 26px;
    min-height: 24px;
    max-height: 24px;
    background: rgba(25, 48, 70, 150);
    border: none;
    border-radius: 8px;
    padding: 0px;
    font-size: 12px;
}

QPushButton#padIconButton:hover {
    background: rgba(45, 108, 152, 170);
    border: none;
}

QPushButton#padDeleteButton {
    min-width: 26px;
    max-width: 26px;
    min-height: 24px;
    max-height: 24px;
    background: rgba(120, 28, 38, 155);
    border: none;
    border-radius: 8px;
    padding: 0px;
    font-size: 12px;
}

QPushButton#padDeleteButton:hover {
    background: rgba(170, 42, 55, 185);
    border: none;
}


QFrame#soundPadCard {
    background: rgba(0, 0, 0, 150);
    border: none;
    border-radius: 12px;
}

QFrame#soundPadCard:hover {
    background: rgba(8, 24, 38, 176);
    border: none;
}

QFrame#soundPadCard[edit="true"] {
    background: rgba(11, 31, 47, 188);
    border: none;
}

QLabel#soundPadIcon {
    min-width: 30px;
    min-height: 24px;
    max-height: 24px;
    background: transparent;
    border: none;
    font-size: 20px;
}

QLabel#soundPadName {
    color: rgba(240, 248, 255, 225);
    font-size: 10px;
    font-weight: 780;
}

QLabel#soundPadMeta {
    color: rgba(205, 224, 242, 140);
    font-size: 8px;
}

QFrame#appsRouteCard {
    background: rgba(0, 0, 0, 138);
    border: none;
    border-radius: 13px;
}

QFrame#appsRouteCard:hover {
    background: rgba(8, 24, 38, 176);
    border: none;
}

QLabel#appRouteIcon {
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
    background: rgba(40, 115, 175, 80);
    border: none;
    border-radius: 15px;
    font-size: 15px;
}

QLabel#appRouteName {
    font-size: 11px;
    font-weight: 820;
}

QLabel#appRouteMeta {
    color: rgba(205, 224, 242, 145);
    font-size: 9px;
}

QLabel#appRouteArrow {
    color: rgba(135, 210, 255, 190);
    font-size: 16px;
    font-weight: 900;
}

QPushButton#appRouteCollapseButton {
    min-width: 26px;
    max-width: 26px;
    min-height: 24px;
    max-height: 24px;
    background: rgba(60, 145, 210, 45);
    border: none;
    border-radius: 12px;
    color: rgba(135, 210, 255, 220);
    font-size: 14px;
    font-weight: 900;
    padding: 0px;
}

QPushButton#appRouteCollapseButton:hover {
    background: rgba(70, 170, 235, 75);
}

QPushButton#soundPadEmoji {
    min-width: 32px;
    max-width: 32px;
    min-height: 28px;
    max-height: 28px;
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0px;
    font-size: 20px;
}

QPushButton#soundPadEmoji:hover {
    background: rgba(40, 80, 110, 95);
    border: none;
}

QLineEdit#soundPadNameEditor {
    background: rgba(0, 0, 0, 130);
    border: none;
    border-radius: 8px;
    color: rgba(240, 248, 255, 235);
    font-size: 10px;
    font-weight: 780;
    padding: 3px 5px;
}

QFrame#emojiPalette {
    background: rgba(0, 0, 0, 150);
    border: none;
    border-radius: 10px;
}

QPushButton#emojiChoice {
    min-width: 24px;
    max-width: 24px;
    min-height: 22px;
    max-height: 22px;
    background: rgba(20, 38, 55, 105);
    border: none;
    border-radius: 7px;
    padding: 0px;
    font-size: 15px;
}

QPushButton#emojiChoice:hover {
    background: rgba(45, 108, 152, 160);
    border: none;
}

QComboBox {
    background: rgba(0, 0, 0, 132);
    border: none;
    border-radius: 9px;
    padding: 4px 8px;
    min-height: 24px;
    font-size: 10px;
}

QComboBox::drop-down {
    border: none;
    width: 18px;
}








QFrame#channelCard[hardGate="true"] {
    background: rgba(56, 18, 128, 78);
    border: none;
}

QFrame#channelCard[hardGate="true"]:hover {
    background: rgba(76, 28, 166, 96);
    border: none;
}


QFrame#channelCard[voiceFxActive="true"] {
    background: rgba(118, 88, 18, 96);
    border: 1px solid rgba(255, 211, 72, 82);
}
QFrame#channelCard[voiceFxActive="true"]:hover {
    background: rgba(142, 105, 22, 116);
    border: 1px solid rgba(255, 222, 96, 112);
}
QPushButton#microFxButton {
    border: 1px solid rgba(92, 190, 238, 34);
    border-radius: 7px;
    background: rgba(42, 112, 156, 34);
    color: rgba(225, 246, 255, 220);
    font-size: 8px;
    font-weight: 760;
    padding: 0px;
}
QPushButton#microFxButton:hover {
    background: rgba(58, 145, 194, 52);
    border: 1px solid rgba(110, 207, 250, 62);
}
QPushButton#microFxButton[fxActive="true"] {
    background: rgba(255, 194, 46, 86);
    border: 1px solid rgba(255, 222, 108, 130);
    color: rgba(255, 252, 232, 250);
}
QLabel#valueLabel {
    min-height: 19px;
    max-height: 19px;
    font-size: 11px;
}

QPushButton#gateButton {
    min-width: 52px;
    max-width: 52px;
    min-height: 20px;
    max-height: 20px;
    border-radius: 7px;
    border: none;
    background: rgba(235, 250, 255, 14);
    color: rgba(238, 250, 255, 192);
    font-size: 8px;
    font-weight: 700;
    padding: 0px;
    margin: 0px;
}

QPushButton#gateButton:hover {
    background: rgba(235, 250, 255, 24);
    border: none;
}

QPushButton#gateButton[gateActive="true"] {
    background: rgba(255, 178, 24, 54);
    border: none;
    color: rgba(255, 250, 232, 220);
}

QPushButton#gateButton[gateActive="true"]:hover {
    background: rgba(255, 196, 48, 70);
    border: none;
}

/* Same geometry as the normal Mute button.
   Only the icon font and colors change in Gate edit mode. */
QPushButton#muteButton[gateToggle="true"] {
    border: none;
    background: rgba(235, 250, 255, 14);
    color: rgba(238, 250, 255, 192);
    font-size: 13px;
    font-weight: 750;
}

QPushButton#muteButton[gateToggle="true"]:hover {
    background: rgba(235, 250, 255, 24);
    border: none;
}

QPushButton#muteButton[gateToggle="true"]:checked,
QPushButton#muteButton[gateToggle="true"][gateActive="true"] {
    background: rgba(72, 255, 142, 58);
    border: none;
    color: rgba(232, 255, 242, 225);
    font-size: 13px;
    font-weight: 750;
}

QPushButton#muteButton[gateToggle="true"][gateActive="true"]:hover {
    background: rgba(92, 255, 162, 76);
    border: none;
}

QSlider::groove:vertical {
    background: rgba(0, 0, 0, 150);
    width: 6px;
    border: none;
    border-radius: 3px;
}

QSlider::handle:vertical {
    background: rgba(108, 211, 255, 240);
    border: none;
    width: 16px;
    height: 12px;
    margin: 0px -5px;
    border-radius: 6px;
}

QSlider::sub-page:vertical {
    background: rgba(0, 0, 0, 180);
    border-radius: 4px;
}

QSlider::add-page:vertical {
    background: rgba(76, 188, 255, 128);
    border-radius: 4px;
}

QSlider::groove:horizontal {
    background: rgba(0, 0, 0, 145);
    height: 6px;
    border: none;
    border-radius: 3px;
}

QSlider::handle:horizontal {
    background: rgba(108, 211, 255, 240);
    border: none;
    width: 18px;
    height: 18px;
    margin: -6px 0px;
    border-radius: 9px;
}

QSlider::sub-page:horizontal {
    background: rgba(82, 190, 255, 120);
    border-radius: 5px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QScrollArea#drawerScroll {
    border: none;
    background: transparent;
}

QScrollBar:vertical,
QScrollBar:horizontal {
    background: transparent;
    width: 6px;
    height: 6px;
}

QScrollBar::handle:vertical,
QScrollBar::handle:horizontal {
    background: rgba(92, 204, 255, 70);
    border-radius: 3px;
}


QPushButton#selectButton {
    background: rgba(0, 0, 0, 132);
    border: none;
    border-radius: 9px;
    padding: 4px 8px;
    min-height: 24px;
    font-size: 10px;
    text-align: center;
}

QPushButton#selectButton:hover {
    background: rgba(14, 32, 50, 150);
    border: none;
}

QLabel#voiceFxStatusPill {
    background: rgba(0, 0, 0, 132);
    border: none;
    border-radius: 9px;
    padding: 4px 8px;
    min-height: 24px;
    color: rgba(235, 245, 252, 225);
    font-size: 10px;
    font-weight: 760;
}

QPushButton#selectButton[deviceMissing="true"] {
    color: rgba(255, 202, 120, 235);
    background: rgba(76, 42, 8, 150);
}

QMenu {
    background: rgba(0, 0, 0, 220);
    color: #edf5ff;
    border: none;
    border-radius: 10px;
    padding: 6px;
}

QMenu::item {
    background: transparent;
    padding: 6px 20px;
    border-radius: 7px;
}

QMenu::item:selected {
    background: rgba(45, 150, 225, 155);
}

QFrame#emojiPalette {
    background: rgba(0, 0, 0, 205);
    border: none;
    border-radius: 14px;
}


QPushButton#padStopAllButton {
    min-width: 34px;
    max-width: 34px;
    min-height: 30px;
    max-height: 30px;
    padding: 0px;
    border-radius: 10px;
    border: 1px solid rgba(255, 125, 135, 130);
    background: rgba(205, 38, 58, 180);
    color: rgba(255, 238, 241, 245);
    font-size: 14px;
    font-weight: 950;
}

QPushButton#padStopAllButton:hover {
    background: rgba(235, 56, 78, 215);
    border: 1px solid rgba(255, 168, 176, 180);
}

QPushButton#padStopAllButton:pressed {
    background: rgba(255, 76, 98, 235);
    border: 1px solid rgba(255, 205, 212, 220);
}

QPushButton#padStopAllButton:disabled {
    background: rgba(118, 30, 42, 135);
    color: rgba(255, 226, 231, 130);
    border: 1px solid rgba(255, 125, 135, 70);
}

QPushButton#padBulkDeleteButton {
    min-width: 32px;
    max-width: 32px;
    min-height: 30px;
    max-height: 30px;
    background: rgba(150, 32, 46, 170);
    border: none;
    border-radius: 15px;
    padding: 0px;
    font-size: 15px;
    font-weight: 900;
}

QPushButton#padBulkDeleteButton:checked {
    background: rgba(35, 165, 92, 175);
    border: none;
}

QPushButton#padCancelButton {
    min-width: 48px;
    min-height: 30px;
    background: rgba(42, 58, 72, 155);
    border: none;
    border-radius: 10px;
    padding: 0px 9px;
    font-size: 10px;
    font-weight: 760;
}


QFrame#soundPadCard[missingSound="true"] {
    border: 1px solid rgba(255, 182, 84, 190);
    background: rgba(75, 35, 18, 105);
}

QFrame#soundPadCard[missingSound="true"] QLabel#soundPadName {
    color: rgba(255, 224, 178, 245);
}

QFrame#soundPadCard[missingSound="true"] QLabel#soundPadMeta {
    color: rgba(255, 190, 96, 235);
    font-weight: 800;
}

QFrame#soundPadCard[missingSound="true"] QPushButton#soundPadEmoji {
    color: rgba(255, 201, 96, 245);
}

QFrame#soundPadCard[bulkSelected="true"] {
    background: rgba(40, 130, 92, 205);
    border: none;
}

QFrame#soundPadCard[bulkSelected="true"]:hover {
    background: rgba(48, 152, 108, 220);
    border: none;
}

QScrollArea#drawerScroll {
    border: none;
    background: transparent;
    padding-right: 10px;
}

QScrollBar:vertical {
    background: rgba(0, 0, 0, 35);
    width: 8px;
    margin: 0px 0px 0px 4px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: rgba(92, 204, 255, 80);
    border-radius: 4px;
    min-height: 28px;
}


/* V16 overrides */
QFrame#soundPadCard[bulkSelected="true"] {
    background: rgba(150, 32, 46, 215);
    border: none;
}

QFrame#soundPadCard[bulkSelected="true"]:hover {
    background: rgba(180, 42, 58, 230);
    border: none;
}

QScrollArea#drawerScroll {
    border: none;
    background: transparent;
    padding-right: 0px;
}

QScrollBar:vertical {
    background: rgba(0, 0, 0, 28);
    width: 7px;
    margin: 0px 0px 0px 3px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: rgba(92, 204, 255, 75);
    border-radius: 4px;
    min-height: 28px;
}


/* V17 scrollbar spacing override */
QScrollArea#drawerScroll {
    border: none;
    background: transparent;
    padding-right: 0px;
}

QScrollBar:vertical {
    background: rgba(0, 0, 0, 24);
    width: 7px;
    margin: 0px 1px 0px 6px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: rgba(92, 204, 255, 75);
    border-radius: 4px;
    min-height: 28px;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}


/* V18 pad compact action buttons */
QPushButton#padIconButton,
QPushButton#padDeleteButton {
    min-width: 22px;
    max-width: 22px;
    min-height: 20px;
    max-height: 20px;
    border: none;
    border-radius: 7px;
    padding: 0px;
    font-size: 10px;
}

QFrame#soundPadActions {
    background: rgba(0, 0, 0, 70);
    border: none;
    border-radius: 9px;
}


/* Glass real soundboard pad text clamp */
QLabel#soundPadName {
    min-height: 17px;
    max-height: 17px;
}

QLabel#soundPadMeta {
    min-height: 13px;
    max-height: 13px;
}


/* Glass final pads polish */
QPushButton#padAddButton,
QPushButton#padBulkDeleteButton {
    qproperty-iconSize: 0px 0px;
    padding: 0px;
    text-align: center;
}

QPushButton#padDeleteButton {
    qproperty-iconSize: 0px 0px;
    padding: 0px;
    text-align: center;
}

QScrollArea#padsGridScroll {
    border: none;
    background: transparent;
    padding-right: 0px;
}


/* Glass pads internal scrollbar */
QScrollBar#padsGridScrollbar:vertical {
    background: rgba(0, 0, 0, 95);
    width: 8px;
    margin: 4px 1px 4px 3px;
    border: none;
    border-radius: 4px;
}

QScrollBar#padsGridScrollbar::handle:vertical {
    background: rgba(92, 204, 255, 170);
    border: none;
    border-radius: 4px;
    min-height: 32px;
}

QScrollBar#padsGridScrollbar::handle:vertical:hover {
    background: rgba(115, 220, 255, 215);
}

QScrollBar#padsGridScrollbar::add-line:vertical,
QScrollBar#padsGridScrollbar::sub-line:vertical {
    height: 0px;
    background: transparent;
    border: none;
}

QScrollBar#padsGridScrollbar::add-page:vertical,
QScrollBar#padsGridScrollbar::sub-page:vertical {
    background: transparent;
    border: none;
}
"""






STYLE += """
/* Glass pad emoji + darkness slider only */
QPushButton#soundPadEmoji {
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    background: rgba(0, 0, 0, 225);
    border: none;
    border-radius: 17px;
    padding: 0px;
    font-size: 20px;
}

QPushButton#soundPadEmoji:hover {
    background: rgba(34, 78, 108, 160);
    border-radius: 17px;
}

QFrame#emojiPalette {
    background: rgba(0, 0, 0, 225);
    border: 1px solid rgba(95, 190, 255, 55);
    border-radius: 18px;
}

QPushButton#emojiChoice {
    border-radius: 10px;
    background: rgba(18, 36, 54, 135);
}

QPushButton#emojiChoice:hover {
    border-radius: 10px;
    background: rgba(50, 120, 170, 175);
}

QFrame#padBgDarknessControls {
    background: rgba(0, 0, 0, 105);
    border: none;
    border-radius: 12px;
}

QSlider#padBgDarknessSlider::groove:horizontal {
    min-height: 6px;
    max-height: 6px;
    border-radius: 3px;
    background: rgba(0, 0, 0, 155);
}

QSlider#padBgDarknessSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -6px 0px;
    border-radius: 8px;
    background: rgba(110, 215, 255, 230);
}

QSlider#padBgDarknessSlider::sub-page:horizontal {
    border-radius: 3px;
    background: rgba(92, 204, 255, 145);
}

QSlider#padBgDarknessSlider::add-page:horizontal {
    border-radius: 3px;
    background: rgba(12, 24, 36, 150);
}
"""


STYLE += """

/* Glass soundboard folder pills */
QFrame#soundboardFolderBar {
    background: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 16px;
}
QPushButton#soundboardFolderPill,
QPushButton#soundboardFolderAddButton {
    min-height: 24px;
    max-height: 24px;
    padding: 2px 10px;
    border-radius: 12px;
    color: rgba(245, 248, 255, 218);
    background: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 22);
}
QPushButton#soundboardFolderPill:hover,
QPushButton#soundboardFolderAddButton:hover {
    background: rgba(255, 255, 255, 28);
    border: 1px solid rgba(255, 255, 255, 42);
}
QPushButton#soundboardFolderPill:checked {
    color: rgba(255, 255, 255, 245);
    background: rgba(120, 170, 255, 76);
    border: 1px solid rgba(170, 205, 255, 120);
}
QPushButton#soundboardFolderAddButton {
    min-width: 28px;
    max-width: 34px;
    padding-left: 0;
    padding-right: 0;
}

/* Glass soundboard volume slider */
QFrame#soundboardVolumeControls {
    background: rgba(0, 0, 0, 105);
    border: none;
    border-radius: 12px;
}

QLabel#soundboardVolumeLabel {
    color: rgba(215, 232, 246, 175);
    font-size: 10px;
    font-weight: 720;
}

QLabel#soundboardVolumeSign {
    color: rgba(215, 232, 246, 190);
    font-size: 12px;
    font-weight: 850;
    min-width: 10px;
    max-width: 10px;
    padding: 0px;
}

QSlider#soundboardVolumeSlider::groove:horizontal {
    min-height: 6px;
    max-height: 6px;
    border-radius: 3px;
    background: rgba(0, 0, 0, 155);
}

QSlider#soundboardVolumeSlider::handle:horizontal {
    width: 16px;
    height: 16px;
    margin: -6px 0px;
    border-radius: 8px;
    background: rgba(110, 215, 255, 230);
}

QSlider#soundboardVolumeSlider::sub-page:horizontal {
    border-radius: 3px;
    background: rgba(92, 204, 255, 145);
}

QSlider#soundboardVolumeSlider::add-page:horizontal {
    border-radius: 3px;
    background: rgba(12, 24, 36, 150);
}

/* compact soundboard auto-level toggle v2 */
QCheckBox#soundboardAutoLevelToggle {
    padding: 2px 5px;
    min-height: 22px;
    max-height: 22px;
    border: none;
    background: transparent;
    font-size: 10px;
    font-weight: 760;
}

QCheckBox#soundboardAutoLevelToggle::indicator {
    width: 11px;
    height: 11px;
}

QPushButton#soundboardAnalyzeButton {
    padding: 0px;
    min-height: 28px;
    max-height: 28px;
    min-width: 30px;
    max-width: 30px;
    border: none;
    background: transparent;
    color: rgba(226, 242, 255, 235);
    font-size: 15px;
    font-weight: 760;
}

QPushButton#soundboardAnalyzeButton:hover {
    background: rgba(35, 82, 112, 90);
}

QPushButton#soundboardAnalyzeButton:disabled {
    color: rgba(185, 205, 220, 130);
    background: transparent;
}


/* Glass drawer/apps scrollbar */
QScrollArea#drawerScroll {
    border: none;
    background: transparent;
    padding-right: 0px;
}

QScrollBar#drawerScrollbar:vertical {
    background: rgba(0, 0, 0, 110);
    width: 8px;
    min-width: 8px;
    margin: 4px 1px 4px 3px;
    border: none;
    border-radius: 4px;
}

QScrollBar#drawerScrollbar::handle:vertical {
    background: rgba(92, 204, 255, 180);
    border: none;
    border-radius: 4px;
    min-height: 32px;
}

QScrollBar#drawerScrollbar::handle:vertical:hover {
    background: rgba(115, 220, 255, 230);
}

QScrollBar#drawerScrollbar::add-line:vertical,
QScrollBar#drawerScrollbar::sub-line:vertical {
    height: 0px;
    background: transparent;
    border: none;
}

QScrollBar#drawerScrollbar::add-page:vertical,
QScrollBar#drawerScrollbar::sub-page:vertical {
    background: transparent;
    border: none;
}
"""

class AdaptiveScrollArea(QScrollArea):
    RIGHT_MARGIN_WITH_SCROLL = 14

    def __init__(self):
        super().__init__()
        self.setObjectName("drawerScroll")
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._applied_right_margin = -1
        self._pending_margin_update = False
        self._lock_content_width = False

        bar = self.verticalScrollBar()
        bar.setObjectName("drawerScrollbar")
        bar.setMinimumWidth(8)
        bar.setSingleStep(36)
        bar.rangeChanged.connect(lambda _minimum, _maximum: self._schedule_margin_update())
        bar.valueChanged.connect(lambda _value: self._schedule_margin_update())

    def setWidget(self, widget) -> None:
        super().setWidget(widget)
        self._schedule_margin_update()

    def set_content_width_locked(self, locked: bool = True) -> None:
        # Some compact drawer pages contain controls with deliberately long
        # labels. Their Qt size hints must never be allowed to widen the whole
        # drawer. When enabled, the page follows the viewport width exactly and
        # scrolling remains vertical-only.
        self._lock_content_width = bool(locked)
        self._schedule_margin_update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_margin_update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_margin_update()

    def _schedule_margin_update(self) -> None:
        if self._pending_margin_update:
            return

        self._pending_margin_update = True
        QTimer.singleShot(0, self._update_scroll_margin)
        QTimer.singleShot(80, self._update_scroll_margin)

    def _update_scroll_margin(self) -> None:
        self._pending_margin_update = False

        bar = self.verticalScrollBar()
        has_scroll = bar.maximum() > bar.minimum()
        right_margin = self.RIGHT_MARGIN_WITH_SCROLL if has_scroll else 0

        if right_margin == self._applied_right_margin:
            return

        self._applied_right_margin = right_margin
        self.setViewportMargins(0, 0, right_margin, 0)

        widget = self.widget()
        if widget is not None:
            widget.updateGeometry()
            if self._lock_content_width:
                viewport_width = max(0, int(self.viewport().width()))
                widget.setMinimumWidth(0)
                widget.setMaximumWidth(viewport_width)
                if viewport_width > 0 and widget.width() != viewport_width:
                    widget.resize(viewport_width, widget.height())
            else:
                widget.adjustSize()

class NoWheelComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setAlignment(Qt.AlignCenter)
        self.lineEdit().setFrame(False)
        self.currentIndexChanged.connect(lambda _idx: self._sync_display_text())

    def wheelEvent(self, event) -> None:
        event.ignore()

    def addItems(self, texts) -> None:
        super().addItems(texts)
        self._sync_display_text()

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        self._sync_display_text()

    def setCurrentText(self, text: str) -> None:
        index = self.findText(text)
        if index >= 0:
            super().setCurrentIndex(index)
        else:
            super().setCurrentText(text)
        self._sync_display_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_display_text()

    def showPopup(self) -> None:
        current = self._full_current_text()
        if self.lineEdit() is not None:
            self.lineEdit().setText(current)
        super().showPopup()

    def hidePopup(self) -> None:
        super().hidePopup()
        self._sync_display_text()

    def _full_current_text(self) -> str:
        if self.currentIndex() >= 0:
            return self.itemText(self.currentIndex())
        return super().currentText()

    def _sync_display_text(self) -> None:
        line = self.lineEdit()
        if line is None:
            return
        full = self._full_current_text()
        width = max(24, line.width() - 8)
        elided = line.fontMetrics().elidedText(full, Qt.ElideRight, width)
        line.blockSignals(True)
        line.setText(elided)
        line.setCursorPosition(0)
        line.blockSignals(False)
        self.setToolTip(full)



class ShortcutKeyEdit(QKeySequenceEdit):
    """One-chord recorder that commits the next key press directly.

    QKeySequenceEdit normally clears its previous sequence as an intermediate
    editing state.  K-Sounds must never persist that transient value, so this
    subclass bypasses Qt's internal multi-step recorder and builds one chord
    directly from the QKeyEvent.

    Return/Enter validates, Backspace/Delete clears, Escape cancels, and
    Tab/Backtab keep focus navigation. Modifier-only/lock keys are ignored.
    Mouse buttons are not part of this keyboard recorder.
    """

    shortcutCommitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        setter = getattr(self, "setMaximumSequenceLength", None)
        if callable(setter):
            setter(1)
        self._capture_original = ""
        self._last_committed = ""
        self._syncing_shortcut = False

    def setShortcutText(self, trigger: str) -> None:
        portable = normalize_shortcut_trigger(trigger)
        self._syncing_shortcut = True
        try:
            self.setKeySequence(QKeySequence(portable))
            self._capture_original = portable
            self._last_committed = portable
        finally:
            self._syncing_shortcut = False

    def focusInEvent(self, event) -> None:
        self._capture_original = self.keySequence().toString(QKeySequence.PortableText)
        self._last_committed = self._capture_original
        super().focusInEvent(event)

    @staticmethod
    def _sequence_from_event(event) -> QKeySequence:
        # Qt 6 exposes the already-normalized QKeyCombination.  Keep a small
        # integer fallback for older PySide6 builds.
        try:
            return QKeySequence(event.keyCombination())
        except Exception:
            modifiers = event.modifiers()
            modifiers_value = int(getattr(modifiers, "value", modifiers))
            return QKeySequence(modifiers_value | int(event.key()))

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in {Qt.Key_Return, Qt.Key_Enter}:
            # Finish/validate the current capture; never bind Enter itself.
            self.clearFocus()
            event.accept()
            return
        if key in {Qt.Key_Backspace, Qt.Key_Delete}:
            self._syncing_shortcut = True
            try:
                self.clear()
            finally:
                self._syncing_shortcut = False
            self._capture_original = ""
            self._last_committed = ""
            self.shortcutCommitted.emit("")
            self.clearFocus()
            event.accept()
            return
        if key == Qt.Key_Escape:
            self.setShortcutText(self._capture_original)
            self.clearFocus()
            event.accept()
            return
        if key in {Qt.Key_Tab, Qt.Key_Backtab}:
            event.ignore()
            self.clearFocus()
            return

        # Modifier-only and lock-state keys should never become product actions.
        reserved_physical = {
            Qt.Key_Shift,
            Qt.Key_Control,
            Qt.Key_Alt,
            Qt.Key_Meta,
            Qt.Key_AltGr,
            Qt.Key_CapsLock,
            Qt.Key_NumLock,
            Qt.Key_ScrollLock,
        }
        if key in reserved_physical:
            event.accept()
            return

        try:
            if event.isAutoRepeat():
                event.accept()
                return
        except Exception:
            pass

        sequence = self._sequence_from_event(event)
        portable = sequence.toString(QKeySequence.PortableText).strip()
        normalized = normalize_shortcut_trigger(portable)
        if not normalized or shortcut_trigger_is_reserved(normalized):
            event.accept()
            return

        # Set the visible value ourselves instead of letting QKeySequenceEdit
        # pass through its transient empty/multi-step capture states.
        self._syncing_shortcut = True
        try:
            self.setKeySequence(QKeySequence(normalized))
        finally:
            self._syncing_shortcut = False

        if normalized != self._last_committed:
            self._last_committed = normalized
            self._capture_original = normalized
            self.shortcutCommitted.emit(normalized)
        QTimer.singleShot(0, self.clearFocus)
        event.accept()


class NoWheelSlider(QSlider):
    def wheelEvent(self, event) -> None:
        event.ignore()





    def _value_from_position(self, pos) -> int:
        minimum = int(self.minimum())
        maximum = int(self.maximum())
        span = max(1, maximum - minimum)

        if self.orientation() == Qt.Horizontal:
            usable = max(1, self.width() - 1)
            ratio = max(0.0, min(1.0, float(pos.x()) / float(usable)))
        else:
            usable = max(1, self.height() - 1)
            ratio = 1.0 - max(0.0, min(1.0, float(pos.y()) / float(usable)))

        return minimum + int(round(ratio * span))

    def _apply_pointer_value(self, event) -> None:
        value = self._value_from_position(event.position().toPoint())
        self.setSliderDown(True)
        if self.value() != value:
            self.setValue(value)
        try:
            self.sliderMoved.emit(value)
        except Exception:
            pass

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._apply_pointer_value(event)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown() and event.buttons() & Qt.LeftButton:
            self._apply_pointer_value(event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.isSliderDown():
            self._apply_pointer_value(event)
            self.setSliderDown(False)
            try:
                self.sliderReleased.emit()
            except Exception:
                pass
            event.accept()
            return
        super().mouseReleaseEvent(event)


DEVICE_NOT_FOUND_SUFFIX = " (not found)"
DEVICE_NOT_FOUND_PREFIX = "⚠ "


def _is_missing_device_label(label: str) -> bool:
    text = str(label or "").strip()
    return text.startswith(DEVICE_NOT_FOUND_PREFIX) and text.endswith(DEVICE_NOT_FOUND_SUFFIX)


def _format_missing_device_label(label: str) -> str:
    clean = str(label or "Unknown device").strip() or "Unknown device"
    if _is_missing_device_label(clean):
        return clean
    return f"{DEVICE_NOT_FOUND_PREFIX}{clean}{DEVICE_NOT_FOUND_SUFFIX}"


def _plain_missing_device_label(label: str) -> str:
    text = str(label or "").strip()
    if _is_missing_device_label(text):
        text = text[len(DEVICE_NOT_FOUND_PREFIX):]
        text = text[: -len(DEVICE_NOT_FOUND_SUFFIX)]
    return text.strip()



STYLE += """
/* Soundboard simple dashboard v2 */
QPushButton#soundboardPanicButton {
    min-height: 43px;
    max-height: 43px;
    min-width: 220px;
    max-width: 340px;
    border-radius: 14px;
    border: 1px solid rgba(255, 90, 120, 125);
    background: rgba(150, 28, 45, 92);
    color: rgba(255, 235, 238, 245);
    font-size: 14px;
    font-weight: 900;
    padding: 3px 12px;
}

QPushButton#soundboardPanicButton:hover {
    background: rgba(190, 38, 58, 125);
    border: 1px solid rgba(255, 125, 145, 170);
}

QPushButton#soundboardSmallIconButton {
    min-width: 34px;
    max-width: 34px;
    min-height: 32px;
    max-height: 32px;
    border-radius: 13px;
    background: rgba(60, 145, 210, 45);
    border: 1px solid rgba(135, 210, 255, 70);
    color: rgba(220, 245, 255, 235);
    font-size: 15px;
    font-weight: 900;
    padding: 0px;
}

QPushButton#soundboardSmallIconButton:hover {
    background: rgba(70, 170, 235, 75);
}
"""

STYLE += """
"""

class SelectButton(QPushButton):
    def __init__(
        self,
        items: list[str],
        current: str | None = None,
        on_change=None,
        parent=None,
        favorite_getter=None,
        favorite_callback=None,
        favorite_other_label: str = "Other devices",
    ):
        super().__init__(parent)
        self.setObjectName("selectButton")
        self.items = list(items)
        self._current = current if current is not None else (self.items[0] if self.items else "")
        self._on_change = on_change
        self._favorite_getter = favorite_getter
        self._favorite_callback = favorite_callback
        self._favorite_other_label = str(favorite_other_label or "Other devices")
        self.clicked.connect(self._open_menu)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sync_text()

    def _item_is_favorite(self, value: str) -> bool:
        getter = self._favorite_getter
        if not callable(getter) or _is_missing_device_label(value):
            return False
        try:
            return bool(getter(str(value or "").strip()))
        except Exception:
            return False

    def _set_item_favorite(self, value: str, favorite: bool) -> bool:
        callback = self._favorite_callback
        if not callable(callback) or _is_missing_device_label(value):
            return False
        try:
            return bool(callback(str(value or "").strip(), bool(favorite)))
        except Exception:
            return False

    def current_text(self) -> str:
        return self._current

    def set_current_text(self, value: str) -> None:
        if value not in self.items and self.items:
            value = self.items[0]
        changed = value != self._current
        self._current = value
        self._sync_text()
        if changed and self._on_change is not None:
            self._on_change(value)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_text()

    def _sync_text(self) -> None:
        missing = _is_missing_device_label(self._current)
        wanted_prop = "true" if missing else "false"
        if self.property("deviceMissing") != wanted_prop:
            self.setProperty("deviceMissing", wanted_prop)
            self.style().unpolish(self)
            self.style().polish(self)
        width = max(28, self.width() - 16)
        text = self.fontMetrics().elidedText(self._current, Qt.ElideRight, width)
        self.setText(text)
        self.setToolTip(_plain_missing_device_label(self._current) if missing else self._current)

    def _open_menu(self) -> None:
        if not self.items:
            return

        menu = QMenu(self)
        menu.setObjectName("compactSelectMenu")
        menu.setStyleSheet("""
QMenu#compactSelectMenu {
    background: rgba(0, 0, 0, 238);
    border: 1px solid rgba(90, 190, 255, 65);
    border-radius: 0px;
    padding: 4px;
}
QLabel#compactSelectSection {
    color: rgba(155, 185, 205, 185);
    background: transparent;
    padding: 6px 8px 3px 8px;
    font-size: 9px;
    font-weight: 800;
}
QLabel#compactSelectSectionFavorites {
    color: rgba(255, 220, 105, 220);
    background: transparent;
    padding: 6px 8px 3px 8px;
    font-size: 9px;
    font-weight: 850;
}
QFrame#compactSelectSectionLine {
    background: rgba(255, 255, 255, 24);
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QLabel#compactSelectItem {
    color: rgba(238, 246, 255, 235);
    background: transparent;
    padding: 5px 8px;
}
QLabel#compactSelectItemMissing {
    color: rgba(255, 202, 120, 235);
    background: rgba(76, 42, 8, 105);
    padding: 5px 8px;
}
QWidget#compactSelectRow {
    background: transparent;
    border-radius: 8px;
}
QWidget#compactSelectRow:hover {
    background: rgba(70, 165, 230, 80);
}
QWidget#compactSelectRow[selected="true"] {
    background: rgba(70, 165, 230, 42);
}
QPushButton#compactSelectFavorite {
    min-width: 27px;
    max-width: 27px;
    min-height: 25px;
    max-height: 25px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: rgba(92, 105, 116, 165);
    font-size: 15px;
    font-weight: 850;
    padding: 0px;
}
QPushButton#compactSelectFavorite:hover {
    background: rgba(255, 210, 82, 25);
    color: rgba(205, 188, 125, 220);
}
QPushButton#compactSelectFavorite:checked {
    background: rgba(255, 210, 82, 30);
    color: rgba(255, 221, 102, 255);
}
QPushButton#compactSelectFavorite:checked:hover {
    background: rgba(255, 210, 82, 48);
    color: rgba(255, 232, 145, 255);
}
QPushButton#compactSelectFavorite:disabled {
    background: transparent;
    color: rgba(78, 86, 94, 95);
}
QScrollArea#compactSelectScroll {
    background: rgba(255, 255, 255, 5);
    border: none;
    border-radius: 7px;
}
QWidget#compactSelectScrollContents {
    background: transparent;
}
QScrollBar#compactSelectScrollBar:vertical {
    background: rgba(255, 255, 255, 10);
    width: 7px;
    margin: 2px 1px 2px 1px;
    border: none;
    border-radius: 3px;
}
QScrollBar#compactSelectScrollBar::handle:vertical {
    background: rgba(92, 204, 255, 125);
    min-height: 26px;
    border: none;
    border-radius: 3px;
}
QScrollBar#compactSelectScrollBar::handle:vertical:hover {
    background: rgba(115, 220, 255, 185);
}
QScrollBar#compactSelectScrollBar::add-line:vertical,
QScrollBar#compactSelectScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
    border: none;
}
QScrollBar#compactSelectScrollBar::add-page:vertical,
QScrollBar#compactSelectScrollBar::sub-page:vertical {
    background: transparent;
}
""")

        favorite_mode = callable(self._favorite_getter) and callable(self._favorite_callback)
        metrics = self.fontMetrics()
        widest = max((metrics.horizontalAdvance(str(item)) for item in self.items), default=self.width())
        # Only favorite-enabled selectors reserve a star column. Generic EQ/APPS
        # selectors stay plain and never leak the device-favorites UI.
        wanted_width = max(self.width(), widest + (52 if favorite_mode else 24), 142)
        wanted_width = min(wanted_width, 340)

        def add_section_header(text: str, *, favorites: bool = False) -> None:
            header = QWidget(menu)
            header_layout = QVBoxLayout(header)
            header_layout.setContentsMargins(3, 1, 3, 1)
            header_layout.setSpacing(2)

            label = QLabel(text, header)
            label.setObjectName("compactSelectSectionFavorites" if favorites else "compactSelectSection")
            label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            header_layout.addWidget(label)

            line = QFrame(header)
            line.setObjectName("compactSelectSectionLine")
            line.setFrameShape(QFrame.NoFrame)
            header_layout.addWidget(line)

            action = QWidgetAction(menu)
            action.setDefaultWidget(header)
            menu.addAction(action)

        def build_device_row(item: str, parent_widget) -> QWidget:
            label_text = str(item)
            missing = _is_missing_device_label(label_text)
            favorite = self._item_is_favorite(label_text)

            row = QWidget(parent_widget)
            row.setObjectName("compactSelectRow")
            row.setProperty("selected", "true" if label_text == self._current else "false")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(4)

            label = QLabel(label_text, row)
            label.setObjectName("compactSelectItemMissing" if missing else "compactSelectItem")
            label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            label.setMinimumHeight(25)
            label.setToolTip(_plain_missing_device_label(label_text) if missing else label_text)
            row_layout.addWidget(label, 1)

            favorite_btn = None
            if favorite_mode:
                # Always use the same filled star glyph. The inactive state stays
                # visible but dark; checked/favorite becomes bright yellow.
                favorite_btn = QPushButton("★", row)
                favorite_btn.setObjectName("compactSelectFavorite")
                favorite_btn.setCheckable(True)
                favorite_btn.setChecked(favorite)
                favorite_btn.setEnabled(not missing and callable(self._favorite_callback))
                favorite_btn.setToolTip("Remove from favorites" if favorite else "Add to favorites")
                row_layout.addWidget(favorite_btn, 0, Qt.AlignVCenter)

            if missing:
                label.setEnabled(False)
            else:
                def select_item(event, value=item):
                    if event.button() == Qt.LeftButton:
                        self.set_current_text(value)
                        menu.close()
                        event.accept()
                        return
                    event.ignore()
                label.mouseReleaseEvent = select_item

                def toggle_favorite(checked, value=item, button=favorite_btn):
                    if not self._set_item_favorite(value, bool(checked)):
                        button.blockSignals(True)
                        restored = self._item_is_favorite(value)
                        button.setChecked(restored)
                        button.setToolTip("Remove from favorites" if restored else "Add to favorites")
                        button.blockSignals(False)
                        return
                    button.setToolTip("Remove from favorites" if checked else "Add to favorites")
                    # Rebuild the open menu immediately: a newly-favorited device
                    # moves out of Other devices and into Favorites, while an
                    # unfavorited device returns to Other devices.
                    QTimer.singleShot(0, populate_menu)
                if favorite_btn is not None:
                    favorite_btn.toggled.connect(toggle_favorite)

            row.setMinimumHeight(29)
            return row

        def add_device_row(item: str) -> None:
            row = build_device_row(item, menu)
            row.setFixedWidth(wanted_width - 8)
            action = QWidgetAction(menu)
            action.setDefaultWidget(row)
            menu.addAction(action)

        def add_scrolling_device_rows(items: list[str]) -> None:
            # Keep the dropdown compact once the non-favorite list grows. Four
            # rows remain visible; additional devices are reached with a real
            # scrollbar instead of stretching the popup down the screen.
            scroll = QScrollArea(menu)
            scroll.setObjectName("compactSelectScroll")
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.verticalScrollBar().setObjectName("compactSelectScrollBar")

            container = QWidget(scroll)
            container.setObjectName("compactSelectScrollContents")
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            row_width = wanted_width - 20
            for item in items:
                row = build_device_row(item, container)
                row.setFixedWidth(row_width)
                layout.addWidget(row)
            layout.addStretch(1)

            visible_rows = min(4, len(items))
            scroll.setFixedWidth(wanted_width - 8)
            scroll.setFixedHeight(max(31, visible_rows * 31 + 2))
            scroll.setWidget(container)

            action = QWidgetAction(menu)
            action.setDefaultWidget(scroll)
            menu.addAction(action)

        def populate_menu() -> None:
            menu.clear()
            available_items = [str(item) for item in self.items]
            favorite_items = [
                item
                for item in available_items
                if not _is_missing_device_label(item) and self._item_is_favorite(item)
            ]
            favorite_set = set(favorite_items)
            other_items = [item for item in available_items if item not in favorite_set]

            if not favorite_mode:
                if len(available_items) > 8:
                    add_scrolling_device_rows(available_items)
                else:
                    for item in available_items:
                        add_device_row(item)
                return

            if favorite_items:
                add_section_header("Favorites", favorites=True)
                for item in favorite_items:
                    add_device_row(item)

            if other_items:
                add_section_header(self._favorite_other_label)
                if len(other_items) > 4:
                    add_scrolling_device_rows(other_items)
                else:
                    for item in other_items:
                        add_device_row(item)

        populate_menu()

        menu.setFixedWidth(wanted_width)

        button_bottom = self.mapToGlobal(self.rect().bottomLeft())
        button_top = self.mapToGlobal(self.rect().topLeft())
        button_center_x = self.mapToGlobal(self.rect().center()).x()

        pos = button_bottom
        pos.setX(button_center_x - wanted_width // 2)

        estimated_height = max(32, int(menu.sizeHint().height()))
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()

            if pos.x() < available.left():
                pos.setX(available.left())
            if pos.x() + wanted_width > available.right():
                pos.setX(max(available.left(), available.right() - wanted_width))

            if pos.y() + estimated_height > available.bottom():
                pos.setY(max(available.top(), button_top.y() - estimated_height))
            if pos.y() < available.top():
                pos.setY(available.top())

        menu.exec(pos)





from ksound_hub.ui._static_blur_label import StaticBlurLabel
from ksound_hub.ui._cached_glass_foreground import CachedGlassForeground


class LevelMeter(QWidget):
    # IP15m5 meter48 POC:
    # - 48 physical-looking LED lens segments on the same -60..0 dBFS scale
    # - dim green/amber/red lens colors remain visible while "off"
    # - lit segments stay color-correct (no digital-white active LED)
    # - near-instant attack + short analogue-style release
    # - short peak hold
    # - perf-first renderer: cache only the 49 possible active-level frames;
    #   peak is one tiny overlay draw, avoiding a level x peak cache explosion.
    MIN_DB = -60.0
    ALIGNMENT_DB = -18.0
    WARNING_DB = -9.0
    DANGER_DB = -3.0

    ALIGNMENT_VISUAL_LEVEL = (ALIGNMENT_DB - MIN_DB) / -MIN_DB
    WARNING_VISUAL_LEVEL = (WARNING_DB - MIN_DB) / -MIN_DB
    DANGER_VISUAL_LEVEL = (DANGER_DB - MIN_DB) / -MIN_DB

    ATTACK_BLEND = 0.96
    RELEASE_BLEND = 0.20

    PEAK_HOLD_TICKS = 6
    PEAK_DECAY = 0.94
    PEAK_SILENCE_DECAY = 0.72
    PEAK_SILENCE_FLOOR = 0.006

    SEGMENTS = 48

    def __init__(self, level: float = 0.0, parent=None):
        super().__init__(parent)

        self.current = self._visual_level(level)
        self.target = self.current
        self.peak = self.current
        self._peak_hold_ticks = 0

        self._background_cache: QPixmap | None = None
        self._level_frame_cache: dict[int, QPixmap] = {}
        self._cache_size: tuple[int, int] = (0, 0)
        self._segment_rects: list[QRectF] = []
        self._last_visual_state: tuple[int, int, bool] | None = None
        # IP15m10 A/B: restrict Qt invalidation to LED rows that actually
        # changed, including previous/new peak lines. Does NOT throttle 40 Hz.
        self._ksh_dirty_regions = os.environ.get('KSH_METER_DIRTY_REGIONS', '1').strip() == '1'
        # IP15m11 A/B: identical integer-pixel LED sprite per segment. All
        # antialiasing happens ONCE in cached pixmaps, never in the 40 Hz painter.
        self._ksh_uniform_leds = os.environ.get('KSH_METER_UNIFORM_LEDS', '1').strip() == '1'
        # IP15m11b opt-in: an identical symmetric double-pointed lens, rasterized
        # once per cached sprite. No geometry, polling, dBFS or audio changes.
        self._ksh_chevron_leds = os.environ.get('KSH_METER_CHEVRON_LEDS', '1').strip() == '1'
        # IP15m11c: opt-in FIXED integer pitch instead of alternating 1/2px gaps.
        # Free space is centered at the extremities; never stretched between LEDs.
        self._ksh_even_grid = os.environ.get('KSH_METER_EVEN_GRID', '0').strip() == '1'
        # IP15m11d: full-length meter, with identical logical segment dimensions
        # and spacing, anti-aliased at cache creation. Independent of native levels.
        self._ksh_fitted_grid = os.environ.get('KSH_METER_FITTED_GRID', '1').strip() == '1'
        self._ksh_led_height = 0
        self._ksh_led_sprites: dict[tuple[bool, int, int, int], QPixmap] = {}
        self._ksh_peak_sprites: dict[tuple[int, int, int], QPixmap] = {}
        self._ksh_fitted_peak_cache: dict[int, tuple[int, QPixmap]] = {}

        self._inactive_colors = (
            QColor(13, 58, 31, 128),
            QColor(17, 72, 36, 136),
            QColor(88, 52, 12, 134),
            QColor(82, 18, 25, 140),
        )

        self._active_colors = (
            QColor(66, 220, 108, 220),
            QColor(103, 244, 132, 235),
            QColor(255, 178, 58, 240),
            QColor(255, 70, 86, 248),
        )

        self._peak_colors = (
            QColor(116, 255, 151, 250),
            QColor(145, 255, 166, 252),
            QColor(255, 210, 92, 252),
            QColor(255, 105, 116, 252),
        )

        # IP15m11e: cosmetic horizontal LED length, configurable once at startup.
        # Fourteen pixels are the default. Fixed-width layout spacing still separates
        # both meters from the volume slider; all cached sprites use self.width().
        _ksh_meter_width = os.environ.get('KSH_METER_LED_WIDTH_PX', '14').strip()
        try:
            _ksh_meter_width = int(_ksh_meter_width)
        except ValueError:
            _ksh_meter_width = 14
        self.setFixedWidth(max(10, min(18, _ksh_meter_width)))
        self.setMinimumHeight(124)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def _visual_level(self, level: float) -> float:
        try:
            raw = float(level)
        except Exception:
            raw = 0.0

        raw = max(0.0, min(1.0, raw))
        if raw <= 0.0:
            return 0.0

        db = 20.0 * math.log10(raw)
        return max(0.0, min(1.0, (db - self.MIN_DB) / -self.MIN_DB))

    def _zone_index_for_segment(self, index: int) -> int:
        segment_level = float(index + 1) / float(self.SEGMENTS)
        if segment_level >= self.DANGER_VISUAL_LEVEL:
            return 3
        if segment_level >= self.WARNING_VISUAL_LEVEL:
            return 2
        if segment_level >= self.ALIGNMENT_VISUAL_LEVEL:
            return 1
        return 0

    def _active_color_for_index(self, index: int) -> QColor:
        return self._active_colors[self._zone_index_for_segment(index)]

    def _inactive_color_for_index(self, index: int) -> QColor:
        return self._inactive_colors[self._zone_index_for_segment(index)]

    def _peak_color_for_index(self, index: int) -> QColor:
        return self._peak_colors[self._zone_index_for_segment(index)]

    def _ksh_uniform_sprite(self, index: int, lit: bool) -> QPixmap:
        """Return a pre-rasterized, symmetric LED lens for the given color zone."""
        width = max(2, self.width())
        led_height = max(1, self._ksh_led_height)
        zone = self._zone_index_for_segment(index)
        key = (bool(lit), zone, width, led_height)
        cached = self._ksh_led_sprites.get(key)
        if cached is not None:
            return cached
        sprite = QPixmap(width, led_height)
        sprite.fill(Qt.transparent)
        p = QPainter(sprite)
        try:
            p.setPen(Qt.NoPen)
            p.setBrush(self._active_colors[zone] if lit else self._inactive_colors[zone])
            if led_height == 1:
                # Single pixel row: no room for a vertically resolved point.
                p.setRenderHint(QPainter.Antialiasing, False)
                p.drawRect(1, 0, max(1, width - 2), 1)
            elif self._ksh_chevron_leds:
                # A 6-vertex, left/right mirror-symmetric pointed lens.
                # Both ends are beveled, never rounded or one-sided.
                # Pixel edges remain inside the pixmap on all DPRs; only the
                # sprite creation is antialiased, not 40 Hz frame painting.
                p.setRenderHint(QPainter.Antialiasing, True)
                left, right = 0.35, float(width) - 0.35
                mid_y = float(led_height) * 0.5
                bevel = min(2.0, max(1.0, (right - left) * 0.25))
                shape = QPainterPath()
                shape.moveTo(left, mid_y)
                shape.lineTo(left + bevel, 0.10)
                shape.lineTo(right - bevel, 0.10)
                shape.lineTo(right, mid_y)
                shape.lineTo(right - bevel, float(led_height) - 0.10)
                shape.lineTo(left + bevel, float(led_height) - 0.10)
                shape.closeSubpath()
                p.drawPath(shape)
            else:
                p.setRenderHint(QPainter.Antialiasing, True)
                radius = min(1.5, led_height * 0.5)
                p.drawRoundedRect(QRectF(0.5, 0.0, float(width - 1),
                                         float(led_height)), radius, radius)
        finally:
            p.end()
        self._ksh_led_sprites[key] = sprite
        return sprite

    def _ksh_uniform_peak_sprite(self, index: int) -> QPixmap:
        """Symmetric 1.5 px peak line cached per color zone, not redrawn."""
        width = max(2, self.width())
        zone = self._zone_index_for_segment(index)
        peak_h = max(1, self._ksh_led_height) if self._ksh_even_grid else 3
        key = (zone, width, peak_h)
        cached = self._ksh_peak_sprites.get(key)
        if cached is not None:
            return cached
        sprite = QPixmap(width, peak_h)
        sprite.fill(Qt.transparent)
        p = QPainter(sprite)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(self._peak_colors[zone])
            if self._ksh_even_grid and peak_h == 1:
                # One physical row cannot fit a double-point; keep it uniform.
                p.setRenderHint(QPainter.Antialiasing, False)
                p.drawRect(1, 0, max(1, width - 2), 1)
            elif self._ksh_chevron_leds:
                # Symmetric six-vertex highlight entirely within the LED row.
                left, right = 0.35, float(width) - 0.35
                bevel = min(2.0, max(1.0, (right - left) * 0.25))
                mid = float(peak_h) * 0.5
                top = 0.10 if self._ksh_even_grid else 0.55
                bottom = float(peak_h) - top if self._ksh_even_grid else 2.45
                shape = QPainterPath()
                shape.moveTo(left, mid)
                shape.lineTo(left + bevel, top)
                shape.lineTo(right - bevel, top)
                shape.lineTo(right, mid)
                shape.lineTo(right - bevel, bottom)
                shape.lineTo(left + bevel, bottom)
                shape.closeSubpath()
                p.drawPath(shape)
            else:
                p.drawRoundedRect(QRectF(0.5, 0.25, float(width - 1), 1.5), 0.75, 0.75)
        finally:
            p.end()
        self._ksh_peak_sprites[key] = sprite
        return sprite

    @staticmethod
    def _ksh_regular_grid(height: int, segments: int) -> tuple[list[int], int, int]:
        """Return bottom-up pixel y, identical LED height, identical pitch.

        Exact spacing over arbitrary integer heights is impossible if all the
        available pixels must be consumed. Center leftover pixels OUTSIDE the
        LED array, rather than giving some intervals an extra pixel.
        """
        n = max(1, int(segments))
        h = max(1, int(height))
        pitch = max(2, (h + 1) // n)
        led_h = max(1, min(3, pitch - 1))
        used_h = (n - 1) * pitch + led_h
        inset = max(0, (h - used_h) // 2)
        return [inset + (n - 1 - i) * pitch for i in range(n)], led_h, pitch

    @staticmethod
    def _ksh_fitted_positions(height: int, segments: int) -> list[tuple[float, float]]:
        """Equal HEIGHT + equal GAP in logical coordinates, filling the widget.

        Fractional geometry is necessary at arbitrary heights. QPainter's AA
        is used only while rasterizing cached frames, NOT every timer tick.
        """
        n = max(1, int(segments))
        h = max(1.0, float(height))
        if n == 1:
            return [(0.5, max(0.1, h - 1.0))]
        gap = min(1.5, max(0.0, (h - 1.0 - n) / (n - 1)))
        led_h = max(0.1, (h - 1.0 - (n - 1) * gap) / n)
        bottom = h - 0.5
        return [(bottom - (i + 1) * led_h - i * gap, led_h) for i in range(n)]

    def _ksh_fitted_path(self, rect: QRectF) -> QPainterPath:
        """Six-vertex left/right symmetric lens, identical per logical LED."""
        width = float(self.width())
        left, right = 0.35, width - 0.35
        bevel = min(2.0, max(0.8, (right - left) * 0.24))
        top = rect.top() + 0.10
        bottom = rect.bottom() - 0.10
        middle = (top + bottom) * 0.5
        shape = QPainterPath()
        shape.moveTo(left, middle)
        shape.lineTo(left + bevel, top)
        shape.lineTo(right - bevel, top)
        shape.lineTo(right, middle)
        shape.lineTo(right - bevel, bottom)
        shape.lineTo(left + bevel, bottom)
        shape.closeSubpath()
        return shape

    def _ksh_draw_fitted_led(self, painter: QPainter, index: int, lit: bool,
                             peak: bool = False) -> None:
        zone = self._zone_index_for_segment(index)
        color = (self._peak_colors[zone] if peak else
                 self._active_colors[zone] if lit else self._inactive_colors[zone])
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(self._ksh_fitted_path(self._segment_rects[index]))

    def _ksh_fitted_peak_sprite(self, index: int) -> tuple[int, QPixmap]:
        """Cache one tiny peak image per LED; no vector drawing at 40 Hz."""
        found = self._ksh_fitted_peak_cache.get(index)
        if found is not None:
            return found
        rect = self._segment_rects[index]
        y0 = max(0, int(math.floor(rect.top() - 1)))
        y1 = min(self.height(), int(math.ceil(rect.bottom() + 1)))
        sprite = QPixmap(max(1, self.width()), max(1, y1 - y0))
        sprite.fill(Qt.transparent)
        painter = QPainter(sprite)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.translate(0.0, -float(y0))
            self._ksh_draw_fitted_led(painter, index, True, peak=True)
        finally:
            painter.end()
        result = (y0, sprite)
        self._ksh_fitted_peak_cache[index] = result
        return result

    def _ensure_cache(self) -> None:
        size = (self.width(), self.height())
        if self._background_cache is not None and self._cache_size == size:
            return

        self._cache_size = size
        self._segment_rects = []
        self._level_frame_cache.clear()
        self._ksh_led_sprites.clear()
        self._ksh_peak_sprites.clear()
        self._ksh_fitted_peak_cache.clear()

        pixmap = QPixmap(max(1, size[0]), max(1, size[1]))
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)

        width = max(2, self.width())
        height = max(2, self.height())

        if self._ksh_fitted_grid:
            # Span the SAME height as the original slider, without the large
            # 17-20 px end margins introduced by integer-pitch IP15m11c.
            # Logical heights/gaps are exactly equal; raster edge coverage is
            # antialiased ONCE, then the normal 49-level frame cache is used.
            painter.setRenderHint(QPainter.Antialiasing, True)
            for i, (y, led_h) in enumerate(self._ksh_fitted_positions(height, self.SEGMENTS)):
                self._segment_rects.append(QRectF(0.0, y, float(width), led_h))
                self._ksh_draw_fitted_led(painter, i, False)
        elif self._ksh_uniform_leds:
            # An integer sprite, reused for all 48 rows, prevents varying
            # thickness and left/right cap shape from fractional rasterization.
            # Spread the unavoidable leftover pixels through the gaps: each
            # lens stays IDENTICAL even at arbitrary Wayland window heights.
            if self._ksh_even_grid:
                # Constant pitch: every 48-row gap is strictly identical.
                y_positions, self._ksh_led_height, _pitch = self._ksh_regular_grid(
                    height, self.SEGMENTS)
            else:
                # Original IP15m11 layout retained for A/B compatibility.
                self._ksh_led_height = max(1, (height - self.SEGMENTS + 1) // self.SEGMENTS)
                available = max(0, height - self._ksh_led_height)
                denominator = max(1, self.SEGMENTS - 1)
                y_positions = [
                    ((self.SEGMENTS - 1 - i) * available + denominator // 2) // denominator
                    for i in range(self.SEGMENTS)
                ]
            for i, y in enumerate(y_positions):
                rect = QRectF(0.0, float(y), float(width), float(self._ksh_led_height))
                self._segment_rects.append(rect)
                painter.drawPixmap(0, y, self._ksh_uniform_sprite(i, False))
        else:
            gap = 1.0
            segment_h = max(
                1.0,
                (height - gap * (self.SEGMENTS - 1)) / max(1, self.SEGMENTS),
            )
            bottom = height - 0.5

            for i in range(self.SEGMENTS):
                y = bottom - (i + 1) * segment_h - i * gap
                rect = QRectF(0.5, y, width - 1.0, segment_h)
                self._segment_rects.append(rect)
                painter.setBrush(self._inactive_color_for_index(i))
                painter.drawRoundedRect(rect, 0.65, 0.65)

        painter.end()
        self._background_cache = pixmap
        self._last_visual_state = None

    def _level_frame_for_active(self, active: int) -> QPixmap:
        self._ensure_cache()

        active = max(0, min(self.SEGMENTS, int(active)))
        cached = self._level_frame_cache.get(active)
        if cached is not None:
            return cached

        size = (max(1, self.width()), max(1, self.height()))
        pixmap = QPixmap(size[0], size[1])
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)

        if self._background_cache is not None:
            painter.drawPixmap(0, 0, self._background_cache)

        if self._ksh_fitted_grid:
            painter.setRenderHint(QPainter.Antialiasing, True)
        for i in range(min(active, len(self._segment_rects))):
            if self._ksh_fitted_grid:
                self._ksh_draw_fitted_led(painter, i, True)
            elif self._ksh_uniform_leds:
                painter.drawPixmap(0, int(self._segment_rects[i].top()),
                                   self._ksh_uniform_sprite(i, True))
            else:
                painter.setBrush(self._active_color_for_index(i))
                painter.drawRoundedRect(self._segment_rects[i], 0.65, 0.65)

        painter.end()

        self._level_frame_cache[active] = pixmap
        return pixmap

    def resizeEvent(self, event) -> None:
        self._background_cache = None
        self._level_frame_cache.clear()
        self._ksh_led_sprites.clear()
        self._ksh_peak_sprites.clear()
        self._ksh_fitted_peak_cache.clear()
        self._last_visual_state = None
        super().resizeEvent(event)

    def set_level(self, level: float) -> None:
        self.target = self._visual_level(level)
        if self.target > self.peak:
            self.peak = self.target
            self._peak_hold_ticks = self.PEAK_HOLD_TICKS

    def reset(self) -> None:
        changed = self.current > 0.0 or self.target > 0.0 or self.peak > 0.0
        self.current = 0.0
        self.target = 0.0
        self.peak = 0.0
        self._peak_hold_ticks = 0
        if changed:
            self.update()

    def _visual_state(self) -> tuple[int, int, bool]:
        active = int(math.ceil(self.current * self.SEGMENTS)) if self.current > 0.0 else 0
        active = max(0, min(self.SEGMENTS, active))

        silent = (
            self.peak <= self.PEAK_SILENCE_FLOOR
            and self.current <= self.PEAK_SILENCE_FLOOR
        )

        if silent:
            peak_index = -1
        elif self.peak >= 1.0:
            peak_index = self.SEGMENTS - 1
        else:
            peak_index = max(
                0,
                min(
                    self.SEGMENTS - 1,
                    int(math.ceil(self.peak * self.SEGMENTS)) - 1,
                ),
            )

        return active, peak_index, silent

    def tick(self) -> None:
        if self.target > self.current:
            blend = self.ATTACK_BLEND
        else:
            blend = self.RELEASE_BLEND

        self.current += (self.target - self.current) * blend

        if self.target <= 0.0 and self.current < self.PEAK_SILENCE_FLOOR:
            self.current = 0.0

        if self._peak_hold_ticks > 0:
            self._peak_hold_ticks -= 1
            self.peak = max(self.peak, self.current)
        elif self.target <= 0.0 and self.current <= 0.0:
            self.peak *= self.PEAK_SILENCE_DECAY
        else:
            self.peak = max(self.current, self.peak * self.PEAK_DECAY)

        if (
            self.target <= 0.0
            and self.current < self.PEAK_SILENCE_FLOOR
            and self.peak < self.PEAK_SILENCE_FLOOR
        ):
            self.peak = 0.0
            self._peak_hold_ticks = 0

        state = self._visual_state()
        if state != self._last_visual_state:
            if self._ksh_dirty_regions:
                self._ksh_update_dirty_regions(state)
            else:
                self.update()

    def _ksh_update_dirty_regions(self, state: tuple[int, int, bool]) -> None:
        """Invalidate only changed pixel rows; keep the original painter.

        Old state is the last ACTUALLY painted frame, not the previous tick:
        updates may be coalesced by the Qt event loop during a busy frame.
        Fallback to a full repaint if the cache or geometry isn't ready.
        """
        previous = self._last_visual_state
        if previous is None or self.width() < 1 or self.height() < 1:
            self.update()
            return
        self._ensure_cache()
        if self._last_visual_state is None or len(self._segment_rects) != self.SEGMENTS:
            self.update()
            return

        old_active, old_peak, old_silent = previous
        new_active, new_peak, new_silent = state
        rects = self._segment_rects
        region = QRegion()
        if old_active != new_active:
            low = max(0, min(old_active, new_active))
            high = min(self.SEGMENTS, max(old_active, new_active))
            if high > low:
                # Geometry is bottom-up; indexing just the endpoints avoids
                # per-segment work in each of the 40 Hz timer ticks.
                top = rects[high - 1].top()
                bottom = rects[low].bottom()
                region = QRegion(
                    QRectF(0.0, top - 1.0, float(self.width()),
                           bottom - top + 2.0).toAlignedRect()
                )

        previous_peak = -1 if old_silent else old_peak
        current_peak = -1 if new_silent else new_peak
        if previous_peak != current_peak:
            for index in (previous_peak, current_peak):
                if 0 <= index < len(rects):
                    peak_y = rects[index].top()
                    peak_span = (rects[index].height() + 2.0) if self._ksh_fitted_grid else 3.5
                    spot = QRectF(0.0, peak_y - 1.0,
                                  float(self.width()), peak_span).toAlignedRect()
                    region = region.united(QRegion(spot))

        region = region.intersected(QRegion(self.rect()))
        if not region.isEmpty():
            self.update(region)
        # Same visual pixels => no update, even if internal float values changed.

    def paintEvent(self, event) -> None:
        state = self._visual_state()
        self._last_visual_state = state

        active, peak_index, silent = state

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.drawPixmap(0, 0, self._level_frame_for_active(active))

        if not silent and self._segment_rects and peak_index >= 0:
            peak_index = max(0, min(len(self._segment_rects) - 1, peak_index))
            peak_y = self._segment_rects[peak_index].top()
            if self._ksh_fitted_grid:
                y, sprite = self._ksh_fitted_peak_sprite(peak_index)
                painter.drawPixmap(0, y, sprite)
            elif self._ksh_uniform_leds:
                painter.drawPixmap(0, int(peak_y), self._ksh_uniform_peak_sprite(peak_index))
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._peak_color_for_index(peak_index))
                painter.drawRect(QRectF(0.0, peak_y, float(self.width()), 1.25))


class TitleBar(QFrame):
    def __init__(self, window: QMainWindow):
        super().__init__()
        self.window = window
        self.setObjectName("titleBar")
        self.setFixedHeight(34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        icon = QLabel("K")
        icon.setObjectName("appIcon")
        icon.setAlignment(Qt.AlignCenter)
        if APP_ICON.is_file():
            pixmap = QPixmap(str(APP_ICON))
            if not pixmap.isNull():
                icon.setPixmap(pixmap.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(icon)

        title = QLabel("K-Sounds Hub")
        title.setObjectName("titleText")
        layout.addWidget(title)

        version = QLabel(APP_BUILD)
        version.setObjectName("versionText")
        version.setToolTip(f"K-Sounds Hub development build {APP_BUILD}")
        layout.addWidget(version)

        layout.addStretch(1)

        min_btn = QPushButton("—")
        min_btn.setObjectName("windowButton")
        min_btn.clicked.connect(window.showMinimized)
        layout.addWidget(min_btn)

        max_btn = QPushButton("□")
        max_btn.setObjectName("windowButton")
        max_btn.clicked.connect(self._toggle_maximized)
        layout.addWidget(max_btn)

        close_btn = QPushButton("×")
        close_btn.setObjectName("closeButton")
        close_btn.clicked.connect(window.close)
        layout.addWidget(close_btn)

    def _toggle_maximized(self) -> None:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            handle = self.window.windowHandle()
            if handle is not None:
                try:
                    if handle.startSystemMove():
                        event.accept()
                        return
                except Exception:
                    pass
            event.accept()


class NavButton(QPushButton):
    def __init__(self, icon: str, label: str):
        super().__init__(f"{icon}\n{label}")
        self.setObjectName("navButton")
        self.setCheckable(True)
        # Seven first-class Glass destinations fit at the minimum window height.
        self.setMinimumHeight(44)




class ChannelCard(QFrame):
    def __init__(
        self,
        name: str,
        icon: str,
        devices: list[str],
        value: int,
        channel_key: str = "",
        volume_callback=None,
        volume_live_callback=None,
        mute_callback=None,
        stereo_width: int = 100,
        stereo_width_callback=None,
        shortcut_state_getter=None,
        shortcut_change_callback=None,
        device_callback=None,
        current_device: str | None = None,
        device_favorite_getter=None,
        device_favorite_callback=None,
        hard_gate_available: bool = False,
        hard_gate_enabled: bool = False,
        hard_gate_threshold: int = 12,
        hard_gate_toggle_callback=None,
        hard_gate_threshold_callback=None,
        micro_processing_callback=None,
        voice_fx_callback=None,
        voice_fx_active: bool = False,
    ):
        super().__init__()
        self.channel_key = str(channel_key or "").strip()
        self._volume_callback = volume_callback
        self._volume_live_callback = volume_live_callback
        self._mute_callback = mute_callback
        self._stereo_width_callback = stereo_width_callback
        self._shortcut_state_getter = shortcut_state_getter
        self._shortcut_change_callback = shortcut_change_callback
        self._shortcut_editors: dict[str, ShortcutKeyEdit] = {}
        self._shortcut_checks: dict[str, QCheckBox] = {}
        self._device_callback = device_callback
        self._device_favorite_getter = device_favorite_getter
        self._device_favorite_callback = device_favorite_callback
        self._hard_gate_available = bool(hard_gate_available and self.channel_key == "micro")
        self._hard_gate_enabled = bool(hard_gate_enabled and self._hard_gate_available)
        self._hard_gate_threshold = max(0, min(100, int(hard_gate_threshold)))
        self._hard_gate_edit_mode = False
        self._hard_gate_toggle_callback = hard_gate_toggle_callback
        self._hard_gate_threshold_callback = hard_gate_threshold_callback
        self._micro_processing_callback = micro_processing_callback if self.channel_key == "micro" else None
        self._voice_fx_callback = voice_fx_callback if self.channel_key == "micro" else None
        self._voice_fx_active = bool(voice_fx_active and self.channel_key == "micro")
        self._normal_volume_value = int(value)
        self._normal_muted_value = False
        self._stereo_width = max(0, min(100, int(round(int(stereo_width) / 5.0) * 5)))
        self._stereo_popover = None
        self._stereo_slider = None
        self._stereo_value_label = None
        self._stereo_save_timer = QTimer(self)
        self._stereo_save_timer.setSingleShot(True)
        self._stereo_save_timer.setInterval(80)
        self._stereo_save_timer.timeout.connect(self._flush_stereo_width_callback)
        self._last_sent_gate_threshold: int | None = None
        self._syncing_controls = False

        # UI-first slider model:
        # - the thumb/label move immediately in the UI;
        # - backend/audio receives the latest value through a short single-shot
        #   timer only while the user is dragging;
        # - release flushes immediately.
        self._pending_volume_value: int | None = None
        self._last_sent_volume_value: int | None = None
        self._last_live_volume_value: int | None = None
        self._volume_apply_timer = QTimer(self)
        self._volume_apply_timer.setSingleShot(True)
        self._volume_apply_timer.setInterval(110)
        self._volume_apply_timer.timeout.connect(self._flush_live_volume_callback)

        # External volume changes (notably global shortcuts) should glide instead
        # of making the vertical thumb jump in 5% chunks.  This timer is UI-only:
        # backend/audio targets are still updated immediately by the controller.
        self._volume_visual_current = float(max(0, min(100, int(value))))
        self._volume_visual_target = self._volume_visual_current
        self._volume_visual_timer = QTimer(self)
        self._volume_visual_timer.setInterval(16)
        self._volume_visual_timer.timeout.connect(self._tick_external_volume_animation)

        self.value = int(value)
        self.setObjectName("channelCard")
        self.setProperty("muted", "false")
        self.setProperty("hardGate", "false")
        self.setProperty("voiceFxActive", "true" if self._voice_fx_active else "false")
        self.setMinimumWidth(168 if self.channel_key == "micro" else 78)
        self.setMaximumWidth(168)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        icon_label = QLabel()
        icon_label.setObjectName("channelIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setCursor(Qt.PointingHandCursor)
        icon_label.setToolTip("Click for stereo width / mono controls.")
        self._set_icon(icon_label, icon)
        self.icon_label = icon_label
        icon_label.mousePressEvent = self._on_icon_mouse_press  # type: ignore[method-assign]
        root.addWidget(icon_label, 0, Qt.AlignHCenter)

        name_label = QLabel(name)
        name_label.setObjectName("channelName")
        name_label.setAlignment(Qt.AlignCenter)
        root.addWidget(name_label)

        self.gate_btn = QPushButton("Hard Gate", self)
        self.gate_btn.setObjectName("gateButton")
        self.gate_btn.setFixedSize(52, 20)
        self.gate_btn.setVisible(self._hard_gate_available)
        self.gate_btn.setToolTip("Edit the independent MICRO Hard Gate using the main slider.")
        self.gate_btn.clicked.connect(self._toggle_hard_gate_edit_mode)
        self.gate_btn.raise_()

        self.fx_btn = QPushButton("Tune", self)
        self.fx_btn.setObjectName("microFxButton")
        self.fx_btn.setFixedSize(46, 20)
        self.fx_btn.setVisible(self.channel_key == "micro" and self._micro_processing_callback is not None)
        self.fx_btn.setToolTip("Open MICRO Tune processing controls, including the smooth Gate.")
        self.fx_btn.clicked.connect(self._on_micro_processing_clicked)
        self.fx_btn.raise_()

        self.voice_fx_btn = QPushButton("VFX", self)
        self.voice_fx_btn.setObjectName("microFxButton")
        self.voice_fx_btn.setFixedSize(46, 20)
        self.voice_fx_btn.setVisible(self.channel_key == "micro" and self._voice_fx_callback is not None)
        self.voice_fx_btn.setToolTip("Open native low-latency MICRO Voice FX.")
        self.voice_fx_btn.clicked.connect(self._on_voice_fx_clicked)
        self.voice_fx_btn.setProperty("fxActive", "true" if self._voice_fx_active else "false")
        self.voice_fx_btn.raise_()

        self.device_select = SelectButton(
            devices,
            current_device or (devices[0] if devices else ""),
            self._device_changed,
            favorite_getter=self._device_label_is_favorite,
            favorite_callback=self._set_device_label_favorite,
        )
        self.device_select.setMinimumWidth(0)
        self.device_select.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.device_select.setToolTip("Choose device. Use ★ inside the menu to add/remove favorites.")
        root.addWidget(self.device_select)

        meter_row = QHBoxLayout()
        meter_row.setContentsMargins(0, 2, 0, 2)
        meter_row.setSpacing(18)
        meter_row.addStretch(1)

        self.left_meter = LevelMeter(0.0)
        self.right_meter = LevelMeter(0.0)
        meter_row.addWidget(self.left_meter)

        self.slider = NoWheelSlider(Qt.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setValue(value)
        self.slider.setMinimumHeight(124)
        self.slider.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.slider.setTracking(False)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._flush_pending_volume_callback)
        self.slider.valueChanged.connect(self._on_volume_changed)
        meter_row.addWidget(self.slider, 0, Qt.AlignHCenter)

        meter_row.addWidget(self.right_meter)
        meter_row.addStretch(1)
        root.addLayout(meter_row, 1)

        self.value_label = QLabel(f"{value}%")
        self.value_label.setObjectName("volumeValue")
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setFixedHeight(19)
        self.value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.value_label)

        self.gate_enable_btn = QPushButton("Gate OFF")
        self.gate_enable_btn.setObjectName("gateEnableButton")
        self.gate_enable_btn.setCheckable(True)
        self.gate_enable_btn.setVisible(False)
        self.gate_enable_btn.toggled.connect(self._on_hard_gate_enabled_changed)

        self.mute_btn = QPushButton("Mute")
        self.mute_btn.setObjectName("muteButton")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFixedHeight(28)
        self.mute_btn.toggled.connect(self._on_muted_changed)
        root.addWidget(self.mute_btn)

        if self._hard_gate_available:
            self.gate_enable_btn.setChecked(self._hard_gate_enabled)
            self._apply_hard_gate_visual_state(sync_slider=True)


    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_gate_button()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_gate_button()

    def _position_gate_button(self) -> None:
        button = getattr(self, "gate_btn", None)
        fx_button = getattr(self, "fx_btn", None)
        voice_fx_button = getattr(self, "voice_fx_btn", None)
        try:
            icon = getattr(self, "icon_label", None)
            icon_y = int(icon.y()) if icon is not None else 10
            top_y = max(7, icon_y + 4)
            right_x = max(5, self.width() - (fx_button.width() if fx_button is not None else 46) - 5)
            if button is not None:
                button.setVisible(bool(self._hard_gate_available))
                button.move(5, top_y)
                button.raise_()
            if fx_button is not None:
                fx_button.move(right_x, top_y)
                fx_button.raise_()
            if voice_fx_button is not None:
                voice_fx_button.move(max(5, self.width() - voice_fx_button.width() - 5), top_y + 23)
                voice_fx_button.raise_()
        except Exception:
            pass

    def _on_micro_processing_clicked(self) -> None:
        callback = self._micro_processing_callback
        if callback is not None:
            callback()

    def _on_voice_fx_clicked(self) -> None:
        callback = self._voice_fx_callback
        if callback is not None:
            callback()

    def set_voice_fx_active(self, active: bool) -> None:
        self._voice_fx_active = bool(active and self.channel_key == "micro")
        self.setProperty("voiceFxActive", "true" if self._voice_fx_active else "false")
        self.voice_fx_btn.setProperty("fxActive", "true" if self._voice_fx_active else "false")
        for widget in (self, self.voice_fx_btn):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()


    def _stereo_width_text(self, value: int) -> str:
        numeric = max(0, min(100, int(value)))
        if numeric == 0:
            return "Mono"
        if numeric == 100:
            return "Stereo"
        return "Narrow"

    def _on_icon_mouse_press(self, event) -> None:
        try:
            if event.button() != Qt.LeftButton:
                return
            event.accept()
        except Exception:
            pass
        self._toggle_stereo_popover()

    def _ensure_stereo_popover(self) -> QFrame:
        parent = self.window() or self
        popover = getattr(self, "_stereo_popover", None)
        if popover is not None and popover.parent() is parent:
            return popover

        if popover is not None:
            try:
                popover.hide()
                popover.deleteLater()
            except Exception:
                pass

        popover = QFrame(parent)
        popover.setObjectName("stereoPopover")
        popover.setAttribute(Qt.WA_StyledBackground, True)
        popover.setWindowFlags(Qt.Widget)

        layout = QVBoxLayout(popover)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        title = QLabel("Stereo width", popover)
        title.setObjectName("stereoPopoverTitle")
        title.setMinimumHeight(18)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("0% Mono  ·  100% Stereo", popover)
        hint.setObjectName("stereoPopoverHint")
        hint.setMinimumHeight(16)
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        mono = QLabel("Mono", popover)
        mono.setObjectName("stereoPopoverHint")
        mono.setMinimumWidth(34)
        row.addWidget(mono)

        slider = NoWheelSlider(Qt.Horizontal, popover)
        slider.setObjectName("stereoPopoverSlider")
        slider.setRange(0, 20)
        slider.setSingleStep(1)
        slider.setPageStep(1)
        slider.setTickPosition(QSlider.NoTicks)
        slider.setTracking(True)
        slider.setMinimumHeight(24)
        slider.setValue(max(0, min(20, int(round(self._stereo_width / 5.0)))))
        slider.valueChanged.connect(self._on_stereo_slider_raw_changed)
        row.addWidget(slider, 1)

        stereo = QLabel("Stereo", popover)
        stereo.setObjectName("stereoPopoverHint")
        stereo.setMinimumWidth(44)
        row.addWidget(stereo)

        layout.addLayout(row)

        value_label = QLabel("", popover)
        value_label.setObjectName("stereoPopoverValue")
        value_label.setMinimumHeight(22)
        value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(value_label)

        divider = QFrame(popover)
        divider.setObjectName("stereoPopoverDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        shortcuts_title = QLabel("Shortcuts", popover)
        shortcuts_title.setObjectName("stereoPopoverShortcutTitle")
        layout.addWidget(shortcuts_title)

        shortcut_specs = (
            ("mute", "Mute"),
            ("voldown", "Vol −"),
            ("volup", "Vol +"),
            ("device_next", "Next ★"),
        )
        for action_key, action_label in shortcut_specs:
            shortcut_row = QHBoxLayout()
            shortcut_row.setContentsMargins(0, 0, 0, 0)
            shortcut_row.setSpacing(6)

            label = QLabel(action_label, popover)
            label.setObjectName("stereoPopoverShortcutLabel")
            label.setFixedWidth(46)
            shortcut_row.addWidget(label)

            editor = ShortcutKeyEdit(popover)
            editor.setObjectName("stereoPopoverShortcutEdit")
            editor.setFixedHeight(26)
            editor.setMinimumWidth(112)
            editor.setToolTip(
                "Click and press one shortcut. Backspace/Delete clears; Enter validates; Esc cancels."
            )

            trigger = ""
            enabled = False
            getter = self._shortcut_state_getter
            if callable(getter):
                try:
                    trigger, enabled = getter(self.channel_key, action_key)
                except Exception:
                    trigger, enabled = "", False
            editor.setShortcutText(str(trigger or ""))
            editor.shortcutCommitted.connect(
                lambda value, action=action_key: self._on_shortcut_trigger_changed(action, value)
            )
            self._shortcut_editors[action_key] = editor
            shortcut_row.addWidget(editor, 1)

            check = QCheckBox(popover)
            check.setObjectName("stereoPopoverShortcutCheck")
            check.setToolTip("Enable this K-Sounds shortcut")
            check.setChecked(bool(enabled and trigger))
            check.setEnabled(bool(trigger))
            check.toggled.connect(
                lambda checked, action=action_key: self._on_shortcut_enabled_changed(action, checked)
            )
            self._shortcut_checks[action_key] = check
            shortcut_row.addWidget(check, 0, Qt.AlignCenter)

            layout.addLayout(shortcut_row)

        self._stereo_slider = slider
        self._stereo_value_label = value_label
        self._sync_stereo_popover_label()

        popover.setFixedSize(284, 295)
        popover.setStyleSheet("""
QFrame#stereoPopover {
    background: rgba(5, 8, 14, 238);
    border: none;
    border-radius: 14px;
}
QLabel#stereoPopoverTitle {
    color: rgba(245, 250, 255, 250);
    background: transparent;
    font-size: 13px;
    font-weight: 900;
}
QLabel#stereoPopoverHint {
    color: rgba(205, 225, 240, 235);
    background: transparent;
    font-size: 11px;
    font-weight: 650;
}
QLabel#stereoPopoverValue {
    color: rgba(255, 255, 255, 250);
    background: transparent;
    font-size: 14px;
    font-weight: 900;
}
QFrame#stereoPopoverDivider {
    background: rgba(255, 255, 255, 34);
    border: none;
}
QLabel#stereoPopoverShortcutTitle {
    color: rgba(225, 238, 248, 245);
    background: transparent;
    font-size: 11px;
    font-weight: 800;
}
QLabel#stereoPopoverShortcutLabel {
    color: rgba(205, 225, 240, 235);
    background: transparent;
    font-size: 11px;
    font-weight: 700;
}
QKeySequenceEdit#stereoPopoverShortcutEdit {
    background: rgba(255, 255, 255, 34);
    color: rgba(250, 253, 255, 255);
    border: 1px solid rgba(190, 225, 245, 90);
    border-radius: 7px;
    padding: 3px 8px;
    min-height: 18px;
    font-size: 11px;
    font-weight: 750;
}
QKeySequenceEdit#stereoPopoverShortcutEdit:hover {
    background: rgba(255, 255, 255, 45);
    border: 1px solid rgba(205, 235, 250, 125);
}
QKeySequenceEdit#stereoPopoverShortcutEdit:focus {
    border: 1px solid rgba(76, 205, 255, 225);
    background: rgba(76, 205, 255, 38);
}
QCheckBox#stereoPopoverShortcutCheck {
    spacing: 0px;
}
QCheckBox#stereoPopoverShortcutCheck::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid rgba(205, 235, 250, 120);
    border-radius: 4px;
    background: rgba(255, 255, 255, 28);
}
QCheckBox#stereoPopoverShortcutCheck::indicator:hover {
    border: 1px solid rgba(76, 205, 255, 190);
    background: rgba(76, 205, 255, 26);
}
QCheckBox#stereoPopoverShortcutCheck::indicator:checked {
    border: 1px solid rgba(76, 205, 255, 235);
    background: rgba(76, 205, 255, 150);
}
QSlider#stereoPopoverSlider::groove:horizontal {
    height: 5px;
    border: none;
    border-radius: 2px;
    background: rgba(255, 255, 255, 50);
}
QSlider#stereoPopoverSlider::sub-page:horizontal {
    height: 5px;
    border: none;
    border-radius: 2px;
    background: rgba(76, 205, 255, 160);
}
QSlider#stereoPopoverSlider::add-page:horizontal {
    height: 5px;
    border: none;
    border-radius: 2px;
    background: rgba(255, 255, 255, 35);
}
QSlider#stereoPopoverSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border: none;
    border-radius: 7px;
    background: rgba(238, 249, 255, 250);
}
""")

        self._stereo_popover = popover
        return popover

    def _on_shortcut_trigger_changed(self, action: str, trigger: str) -> None:
        callback = self._shortcut_change_callback
        if callable(callback):
            callback(self.channel_key, str(action), trigger=str(trigger or ""))

    def _on_shortcut_enabled_changed(self, action: str, enabled: bool) -> None:
        callback = self._shortcut_change_callback
        if callable(callback):
            callback(self.channel_key, str(action), enabled=bool(enabled))

    def sync_shortcut_binding(self, action: str, trigger: str, enabled: bool) -> None:
        editor = self._shortcut_editors.get(str(action))
        check = self._shortcut_checks.get(str(action))
        clean = str(trigger or "").strip()
        if editor is not None:
            editor.blockSignals(True)
            editor.setShortcutText(clean)
            editor.blockSignals(False)
        if check is not None:
            check.blockSignals(True)
            check.setEnabled(bool(clean))
            check.setChecked(bool(clean) and bool(enabled))
            check.blockSignals(False)

    def _sync_stereo_popover_label(self) -> None:
        label = getattr(self, "_stereo_value_label", None)
        if label is not None:
            label.setText(f"{self._stereo_width_text(self._stereo_width)}  {int(self._stereo_width)}%")

    def _position_stereo_popover(self) -> None:
        popover = getattr(self, "_stereo_popover", None)
        icon = getattr(self, "icon_label", None)
        if popover is None or icon is None:
            return

        parent = popover.parentWidget()
        if parent is None:
            return

        try:
            pos = icon.mapTo(parent, QPoint(0, icon.height() + 7))
            x = max(8, min(pos.x() - (popover.width() - icon.width()) // 2, parent.width() - popover.width() - 8))
            y = max(8, min(pos.y(), parent.height() - popover.height() - 8))
            popover.move(x, y)
        except Exception:
            popover.move(12, 12)

    def _toggle_stereo_popover(self) -> None:
        popover = self._ensure_stereo_popover()
        if popover.isVisible():
            self._hide_stereo_popover()
            return
        slider = getattr(self, "_stereo_slider", None)
        if slider is not None:
            slider.blockSignals(True)
            slider.setValue(max(0, min(20, int(round(self._stereo_width / 5.0)))))
            slider.blockSignals(False)
        self._sync_stereo_popover_label()
        popover.adjustSize()
        self._position_stereo_popover()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        popover.show()
        popover.raise_()

    def _hide_stereo_popover(self) -> None:
        popover = getattr(self, "_stereo_popover", None)
        if popover is not None:
            popover.hide()
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass

    def _on_stereo_slider_raw_changed(self, raw: int) -> None:
        self._stereo_width = max(0, min(100, int(raw) * 5))
        self._sync_stereo_popover_label()
        self._stereo_save_timer.start()

    def _flush_stereo_width_callback(self) -> None:
        if self._stereo_width_callback is None or not self.channel_key:
            return
        try:
            self._stereo_width_callback(self.channel_key, int(self._stereo_width))
        except Exception:
            pass

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.MouseButtonPress:
            popover = getattr(self, "_stereo_popover", None)
            if popover is not None and popover.isVisible():
                try:
                    global_pos = event.globalPosition().toPoint()
                except AttributeError:
                    global_pos = event.globalPos()

                popover_rect = QRect(popover.mapToGlobal(QPoint(0, 0)), popover.size())
                icon = getattr(self, "icon_label", None)
                icon_rect = QRect(icon.mapToGlobal(QPoint(0, 0)), icon.size()) if icon is not None else QRect()

                if not popover_rect.contains(global_pos) and not icon_rect.contains(global_pos):
                    self._hide_stereo_popover()

        return super().eventFilter(obj, event)

    def _set_icon(self, icon_label: QLabel, icon: str) -> None:
        icon_path = Path(str(icon or ""))
        if icon_path.is_file():
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                icon_label.setPixmap(pixmap.scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                icon_label.setFixedSize(48, 48)
                return
        icon_label.setText(str(icon or ""))
        icon_label.setFixedSize(48, 48)

    def set_meters_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if not visible:
            self.left_meter.reset()
            self.right_meter.reset()
        self.left_meter.setVisible(visible)
        self.right_meter.setVisible(visible)


    def _set_volume_label(self, value: int) -> None:
        clamped = max(0, min(100, int(value)))
        if self._hard_gate_available and self._hard_gate_edit_mode:
            self._hard_gate_threshold = clamped
            self.value_label.setText(f"Gate {self._hard_gate_threshold}%")
            return

        self.value = clamped
        self._normal_volume_value = clamped
        self.value_label.setText(f"{self.value}%")

    def _send_volume_callback_value(self, value: int) -> bool:
        if self._syncing_controls or not self.channel_key:
            return True

        try:
            clamped = max(0, min(100, int(value)))
        except Exception:
            return False

        if self._hard_gate_available and self._hard_gate_edit_mode:
            if self._hard_gate_threshold_callback is None:
                return True
            if self._last_sent_gate_threshold == clamped:
                return True
            self._last_sent_gate_threshold = clamped
            ok = bool(self._hard_gate_threshold_callback(clamped))
            if not ok:
                self.setToolTip("K-Sounds hard gate control is not reachable.")
            return ok

        if self._volume_callback is None:
            return True

        if self._last_sent_volume_value == clamped:
            return True

        self._last_sent_volume_value = clamped
        ok = bool(self._volume_callback(self.channel_key, clamped))
        if not ok:
            self.setToolTip("K-Sounds real app IPC is not reachable.")
        return ok


    def _queue_volume_callback(self, value: int, *, immediate: bool = False) -> None:
        if self._syncing_controls or not self.channel_key:
            return
        if not (self._hard_gate_available and self._hard_gate_edit_mode) and self._volume_callback is None:
            return

        self._pending_volume_value = max(0, min(100, int(value)))

        if self.slider.isSliderDown() and not immediate:
            # Gate edit mode: the slider edits a saved threshold, not live volume.
            if self._hard_gate_available and self._hard_gate_edit_mode:
                return

            # Silent live apply only updates the audio path. It does not emit
            # overlay/status/save churn while the pointer is moving.
            if self._volume_live_callback is not None and not self._volume_apply_timer.isActive():
                self._volume_apply_timer.start()
            return

        self._flush_pending_volume_callback()

    def _flush_live_volume_callback(self) -> None:
        if self._syncing_controls or self._volume_live_callback is None or not self.channel_key:
            return
        if not self.slider.isSliderDown():
            return

        value = self._pending_volume_value
        if value is None:
            return

        try:
            volume = max(0, min(100, int(value)))
        except Exception:
            return

        if self._last_live_volume_value == volume:
            return

        self._last_live_volume_value = volume
        try:
            self._volume_live_callback(self.channel_key, volume)
        except Exception:
            pass

    def _flush_pending_volume_callback(self) -> None:
        if self._syncing_controls:
            return

        timer = getattr(self, "_volume_apply_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

        value = self._pending_volume_value
        self._pending_volume_value = None
        if value is None:
            return

        self._send_volume_callback_value(value)
        self._last_live_volume_value = None

    def _on_slider_moved(self, value: int) -> None:
        self._set_volume_label(value)
        self._queue_volume_callback(value)

    def _on_volume_changed(self, value: int) -> None:
        self._set_volume_label(value)
        self._queue_volume_callback(value, immediate=not self.slider.isSliderDown())

    def _apply_muted_style(self, checked: bool) -> None:
        self.setProperty("muted", "true" if checked else "false")
        self.mute_btn.setText("Muted" if checked else "Mute")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


    def _gate_button_text(self) -> str:
        return "Gate"



    def _apply_hard_gate_visual_state(self, *, sync_slider: bool = False) -> None:
        if not self._hard_gate_available:
            return

        # Full card yellow only while editing Gate.
        self.setProperty("hardGate", "true" if self._hard_gate_edit_mode else "false")
        if self._hard_gate_edit_mode:
            self.setStyleSheet("""
                QFrame#channelCard {
                    background: rgba(56, 18, 128, 78);
                    border: none;
                    border-radius: 15px;
                }
                QFrame#channelCard:hover {
                    background: rgba(76, 28, 166, 96);
                    border: none;
                }
            """)
        else:
            self.setStyleSheet("")

        self.gate_btn.setProperty("gateActive", "true" if self._hard_gate_enabled else "false")
        self.gate_btn.setText("Hard Gate")

        self.gate_enable_btn.setVisible(False)

        if self._hard_gate_edit_mode:
            self.slider.setToolTip("MICRO hard gate threshold")
            value = self._hard_gate_threshold
            label = f"Gate {value}%"

            self.mute_btn.setProperty("gateToggle", "true")
            self.mute_btn.setProperty("gateActive", "true" if self._hard_gate_enabled else "false")
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(bool(self._hard_gate_enabled))
            self.mute_btn.blockSignals(False)
            self.mute_btn.setText("⏻")
            self.mute_btn.setToolTip("Enable or disable the MICRO input hard gate.")
        else:
            self.slider.setToolTip("MICRO input volume")
            value = self._normal_volume_value
            label = f"{value}%"

            self.mute_btn.setProperty("gateToggle", "false")
            self.mute_btn.setProperty("gateActive", "false")
            self.mute_btn.blockSignals(True)
            self.mute_btn.setChecked(bool(self._normal_muted_value))
            self.mute_btn.blockSignals(False)
            self.mute_btn.setText("Muted" if self._normal_muted_value else "Mute")
            self.mute_btn.setToolTip("Mute MICRO.")
            self.setProperty("muted", "true" if self._normal_muted_value else "false")

        if sync_slider:
            self._syncing_controls = True
            try:
                self.slider.blockSignals(True)
                self.slider.setValue(max(0, min(100, int(value))))
                self.slider.blockSignals(False)
                self.value_label.setText(label)
            finally:
                self._syncing_controls = False
        else:
            self.value_label.setText(label)

        for widget in (self, self.gate_btn, self.mute_btn):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        self._position_gate_button()


    def _toggle_hard_gate_edit_mode(self) -> None:
        if not self._hard_gate_available:
            return

        if self._hard_gate_edit_mode:
            self._flush_pending_volume_callback()
            self._hard_gate_edit_mode = False
        else:
            self._normal_volume_value = int(self.value)
            self._normal_muted_value = bool(self.mute_btn.isChecked())
            self._last_sent_gate_threshold = None
            self._hard_gate_edit_mode = True

        self._apply_hard_gate_visual_state(sync_slider=True)


    def _on_hard_gate_enabled_changed(self, checked: bool) -> None:
        if self._syncing_controls or not self._hard_gate_available:
            return

        self._hard_gate_enabled = bool(checked)
        self._apply_hard_gate_visual_state(sync_slider=False)

        if self._hard_gate_toggle_callback is not None:
            ok = bool(self._hard_gate_toggle_callback(bool(checked)))
            if not ok:
                self.setToolTip("K-Sounds hard gate toggle is not reachable.")



    def _on_muted_changed(self, checked: bool) -> None:
        if self._hard_gate_available and self._hard_gate_edit_mode:
            if self._syncing_controls:
                return

            self._hard_gate_enabled = bool(checked)
            self._apply_hard_gate_visual_state(sync_slider=False)

            if self._hard_gate_toggle_callback is not None:
                ok = bool(self._hard_gate_toggle_callback(bool(checked)))
                if not ok:
                    self.setToolTip("K-Sounds hard gate toggle is not reachable.")
            return

        self._normal_muted_value = bool(checked)
        self._apply_muted_style(bool(checked))
        if self._syncing_controls or self._mute_callback is None or not self.channel_key:
            return
        ok = bool(self._mute_callback(self.channel_key, bool(checked)))
        if not ok:
            self.setToolTip("K-Sounds real app IPC is not reachable.")


    def _device_changed(self, label: str) -> None:
        if getattr(self, "_syncing_controls", False):
            return
        if self._device_callback is not None:
            self._device_callback(self.channel_key, str(label or "").strip())

    def _device_label_is_favorite(self, label: str) -> bool:
        value = str(label or "").strip()
        if not value or _is_missing_device_label(value):
            return False
        getter = self._device_favorite_getter
        if not callable(getter):
            return False
        try:
            return bool(getter(self.channel_key, value))
        except Exception:
            return False

    def _set_device_label_favorite(self, label: str, favorite: bool) -> bool:
        value = str(label or "").strip()
        if not value or _is_missing_device_label(value):
            return False
        callback = self._device_favorite_callback
        if not callable(callback):
            return False
        try:
            return bool(callback(self.channel_key, value, bool(favorite)))
        except Exception:
            return False

    def sync_device_choices(self, devices: list[str], current_device: str | None = None) -> None:
        items = [str(item or "").strip() for item in (devices or []) if str(item or "").strip()]
        current = str(current_device or "").strip()
        if current and current not in items:
            items.insert(0, current)
        if not current and items:
            current = items[0]

        self._syncing_controls = True
        try:
            self.device_select.items = items
            self.device_select._current = current
            self.device_select._sync_text()
        finally:
            self._syncing_controls = False



    def render_shortcut_volume(self, value: int) -> None:
        """Hot-path renderer used by global volume shortcuts.

        Deliberately updates only the volume thumb/label. It must not resync
        mute state, repolish the card, run callbacks, or start the legacy
        external-volume animation.
        """
        value = max(0, min(100, int(value)))
        if self._hard_gate_available and self._hard_gate_edit_mode:
            return
        self._volume_visual_timer.stop()
        self._volume_visual_current = float(value)
        self._volume_visual_target = float(value)
        self._normal_volume_value = value
        self._last_sent_volume_value = value
        self.value = value
        if self.slider.value() != value:
            self.slider.blockSignals(True)
            try:
                self.slider.setValue(value)
            finally:
                self.slider.blockSignals(False)
        text = f"{value}%"
        if self.value_label.text() != text:
            self.value_label.setText(text)

    def _render_external_volume_visual(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        self.value = value
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.value_label.setText(f"{value}%")

    def _tick_external_volume_animation(self) -> None:
        if self.slider.isSliderDown() or (self._hard_gate_available and self._hard_gate_edit_mode):
            self._volume_visual_timer.stop()
            return

        target = float(self._volume_visual_target)
        current = float(self._volume_visual_current)
        delta = target - current
        if abs(delta) < 0.45:
            self._volume_visual_current = target
            self._render_external_volume_visual(round(target))
            self._volume_visual_timer.stop()
            return

        # Exponential catch-up gives a smooth 60 Hz glide for single 5% taps,
        # while still catching up quickly when a volume key is held.
        current += delta * 0.38
        self._volume_visual_current = current
        self._render_external_volume_visual(round(current))

    def sync_from_saved_state(
        self,
        *,
        volume: int | None = None,
        muted: bool | None = None,
        hard_gate_enabled: bool | None = None,
        hard_gate_threshold: int | None = None,
        animate_volume: bool = False,
    ) -> None:
        self._syncing_controls = True
        try:
            if hard_gate_threshold is not None and self._hard_gate_available:
                try:
                    self._hard_gate_threshold = max(0, min(100, int(hard_gate_threshold)))
                except Exception:
                    self._hard_gate_threshold = 12

            if hard_gate_enabled is not None and self._hard_gate_available:
                self._hard_gate_enabled = bool(hard_gate_enabled)

            if volume is not None:
                try:
                    value = max(0, min(100, int(volume)))
                except Exception:
                    value = self.value
                self._normal_volume_value = value
                self._last_sent_volume_value = value
                self._last_live_volume_value = None
                self._volume_visual_target = float(value)
                if not animate_volume or self.slider.isSliderDown() or (self._hard_gate_available and self._hard_gate_edit_mode):
                    self._volume_visual_timer.stop()
                    self._volume_visual_current = float(value)
                    self.value = value

            if muted is not None:
                self._normal_muted_value = bool(muted)

            current = self._hard_gate_threshold if (
                self._hard_gate_available and self._hard_gate_edit_mode
            ) else self._normal_volume_value

            if animate_volume and volume is not None and not self.slider.isSliderDown() and not (self._hard_gate_available and self._hard_gate_edit_mode):
                if not self._volume_visual_timer.isActive():
                    self._volume_visual_current = float(self.slider.value())
                    self._volume_visual_timer.start()
            else:
                self.slider.blockSignals(True)
                self.slider.setValue(max(0, min(100, int(current))))
                self.slider.blockSignals(False)

            if self._hard_gate_available:
                self._apply_hard_gate_visual_state(sync_slider=False)
            else:
                self.mute_btn.blockSignals(True)
                self.mute_btn.setChecked(bool(self._normal_muted_value))
                self.mute_btn.blockSignals(False)
                self._apply_muted_style(bool(self._normal_muted_value))
                if not (animate_volume and volume is not None):
                    self.value_label.setText(f"{self.value}%")
        finally:
            self._syncing_controls = False


    def set_meter_levels(self, left: float, right: float) -> None:
        self.left_meter.set_level(left)
        self.right_meter.set_level(right)

    def tick_meters(self) -> None:
        self.left_meter.tick()
        self.right_meter.tick()





class AppRouteCard(QFrame):
    def __init__(self, stream, move_callback):
        super().__init__()
        self.stream = stream
        self.stream_id = int(getattr(stream, "stream_id", -1))
        self._move_callback = move_callback
        self._moving = False

        self.setObjectName("appsRouteCard")
        self.setMinimumHeight(66)

        root = QHBoxLayout(self)
        root.setContentsMargins(9, 8, 9, 8)
        root.setSpacing(8)

        icon_label = QLabel(self._icon_for_stream(stream))
        icon_label.setObjectName("appRouteIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        root.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)

        name_label = QLabel(self._stream_name(stream))
        name_label.setObjectName("appRouteName")
        name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        name_label.setToolTip(self._tooltip_for_stream(stream))
        text_col.addWidget(name_label)

        self.meta_label = QLabel(self._stream_meta(stream))
        self.meta_label.setObjectName("appRouteMeta")
        self.meta_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.meta_label.setToolTip(self._tooltip_for_stream(stream))
        text_col.addWidget(self.meta_label)

        root.addLayout(text_col, 1)

        arrow = QLabel("→")
        arrow.setObjectName("appRouteArrow")
        arrow.setAlignment(Qt.AlignCenter)
        root.addWidget(arrow)

        current_key = str(getattr(stream, "sink_name", "") or "").strip()
        current_label = APP_ROUTE_LABEL_BY_KEY.get(current_key, "ALL")
        self.select = SelectButton([label for label, _key in APP_ROUTE_CHANNELS], current_label, self._channel_changed)
        self.select.setMinimumWidth(70)
        self.select.setMaximumWidth(82)
        root.addWidget(self.select)

    def _stream_name(self, stream) -> str:
        return str(getattr(stream, "display_name", "") or f"Stream {getattr(stream, 'stream_id', '?')}")

    def _stream_meta(self, stream) -> str:
        parts = [f"#{getattr(stream, 'stream_id', '?')}"]
        sink = str(getattr(stream, "sink_name", "") or "").strip()
        parts.append(f"now: {APP_ROUTE_LABEL_BY_KEY.get(sink, sink or 'unknown')}")

        binary_name = str(getattr(stream, "binary_name", "") or "").strip()
        app_name = str(getattr(stream, "app_name", "") or "").strip()
        media_name = str(getattr(stream, "media_name", "") or "").strip()

        if binary_name:
            parts.append(f"bin: {binary_name}")
        elif app_name:
            parts.append(app_name)
        elif media_name:
            parts.append(media_name)

        return " · ".join(parts)

    def _tooltip_for_stream(self, stream) -> str:
        values = [
            ("stream", getattr(stream, "stream_id", "")),
            ("sink", getattr(stream, "sink_name", "")),
            ("display", getattr(stream, "display_name", "")),
            ("app", getattr(stream, "app_name", "")),
            ("bin", getattr(stream, "binary_name", "")),
            ("media", getattr(stream, "media_name", "")),
            ("node", getattr(stream, "node_name", "")),
        ]
        return "\n".join(f"{key}: {value}" for key, value in values if str(value or "").strip())

    def _icon_for_stream(self, stream) -> str:
        haystack = " ".join(
            str(value or "").lower()
            for value in (
                getattr(stream, "display_name", ""),
                getattr(stream, "app_name", ""),
                getattr(stream, "binary_name", ""),
                getattr(stream, "media_name", ""),
                getattr(stream, "node_name", ""),
            )
        )

        if any(word in haystack for word in ("steam", "game", "proton", "wine")):
            return "🎮"
        if any(word in haystack for word in ("firefox", "chrome", "browser", "youtube")):
            return "🌐"
        if any(word in haystack for word in ("discord", "vesktop", "chat")):
            return "💬"
        if any(word in haystack for word in ("spotify", "music", "vlc", "media")):
            return "🎵"
        if "soundboard" in haystack:
            return "🎛"
        return "▣"

    def _channel_changed(self, label: str) -> None:
        if self._moving:
            return

        target = APP_ROUTE_KEY_BY_LABEL.get(str(label or "").strip())
        if not target:
            return

        if target == str(getattr(self.stream, "sink_name", "") or "").strip():
            return

        self._moving = True
        self.select.setEnabled(False)
        self.meta_label.setText(f"Moving to {label}…")

        ok = False
        if self._move_callback is not None:
            ok = bool(self._move_callback(self.stream_id, target))

        if not ok:
            self.meta_label.setText("Move failed")
            self.select.setToolTip("PipeWire refused the move, or the stream disappeared.")
        else:
            self.meta_label.setText(f"Moved to {label}")

        QTimer.singleShot(350, self._unlock_after_move)

    def _unlock_after_move(self) -> None:
        self._moving = False
        self.select.setEnabled(True)



class PermanentRouteCard(QFrame):
    def __init__(
        self,
        icon: str,
        title: str,
        meta: str,
        *,
        checks: list[tuple[str, bool, object]] | None = None,
        sliders: list[tuple[str, int, int, int, object]] | None = None,
        select_items: list[str] | None = None,
        select_current: str | None = None,
        select_callback=None,
        collapsible: bool = False,
        expanded: bool = True,
        expanded_changed=None,
    ):
        super().__init__()
        self.setObjectName("appsRouteCard")
        self.setMinimumHeight(66)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self._collapsible = bool(collapsible)
        self._expanded = bool(expanded) if self._collapsible else True
        self._expanded_changed = expanded_changed
        self._open_minimum_height = 66
        self._collapse_button = None

        root = QVBoxLayout(self)
        root.setContentsMargins(9, 8, 9, 8)
        root.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        icon_label = QLabel(icon)
        icon_label.setObjectName("appRouteIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        header.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        name_label = QLabel(title)
        name_label.setObjectName("appRouteName")
        text_col.addWidget(name_label)

        meta_label = QLabel(meta)
        meta_label.setObjectName("appRouteMeta")
        meta_label.setWordWrap(True)
        text_col.addWidget(meta_label)

        header.addLayout(text_col, 1)

        if self._collapsible:
            self._collapse_button = QPushButton()
            self._collapse_button.setObjectName("appRouteCollapseButton")
            self._collapse_button.setCursor(Qt.PointingHandCursor)
            self._collapse_button.clicked.connect(self._toggle_expanded)
            header.addWidget(self._collapse_button)

        root.addLayout(header)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(7)

        if select_items:
            select_row = QHBoxLayout()
            select_row.setContentsMargins(0, 0, 0, 0)
            select_row.setSpacing(8)

            label = QLabel("Output")
            label.setObjectName("appRouteMeta")
            select_row.addWidget(label)

            self.select = SelectButton(select_items, select_current or select_items[0], select_callback)
            self.select.setMinimumWidth(150)
            select_row.addWidget(self.select, 1)
            body.addLayout(select_row)

        if checks:
            checks_grid = QGridLayout()
            checks_grid.setContentsMargins(0, 0, 0, 0)
            checks_grid.setHorizontalSpacing(10)
            checks_grid.setVerticalSpacing(6)

            for index, (label, checked, callback) in enumerate(checks):
                box = QCheckBox(label)
                box.setObjectName("routeCheck")
                box.setMinimumWidth(82)
                box.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
                box.setChecked(bool(checked))
                box.toggled.connect(lambda value, cb=callback: cb(bool(value)) if cb is not None else None)
                checks_grid.addWidget(box, index // 2, index % 2)

            self._open_minimum_height = max(self._open_minimum_height, 132 if len(checks) > 2 else 78)
            body.addLayout(checks_grid)

        if sliders:
            for label_text, value, minimum, maximum, callback in sliders:
                slider_row = QHBoxLayout()
                slider_row.setContentsMargins(0, 0, 0, 0)
                slider_row.setSpacing(8)

                label = QLabel(label_text)
                label.setObjectName("appRouteMeta")
                slider_row.addWidget(label)

                value_label = QLabel(f"{int(value)}%")
                value_label.setObjectName("appRouteMeta")
                value_label.setMinimumWidth(42)
                value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

                slider = QSlider(Qt.Horizontal)
                slider.setMinimum(int(minimum))
                slider.setMaximum(int(maximum))
                slider.setValue(max(int(minimum), min(int(maximum), int(value))))
                slider.setToolTip(label_text)

                apply_timer = QTimer(slider)
                apply_timer.setSingleShot(True)
                apply_timer.setInterval(260)

                def apply_value(sl=slider, cb=callback):
                    if cb is not None:
                        cb(int(sl.value()))

                apply_timer.timeout.connect(apply_value)

                def on_value_changed(raw, sl=slider, value_node=value_label, timer=apply_timer):
                    value_node.setText(f"{int(raw)}%")
                    # Avoid hammering PipeWire/pactl with many overlapping volume ramps
                    # while the user drags quickly. The label remains live; audio applies
                    # when the slider is released, or after a short debounce for non-drag
                    # changes such as keyboard/page clicks.
                    if sl.isSliderDown():
                        timer.stop()
                    else:
                        timer.start()

                def on_slider_released(timer=apply_timer):
                    timer.stop()
                    timer.start()

                slider.valueChanged.connect(on_value_changed)
                slider.sliderReleased.connect(on_slider_released)

                slider_row.addWidget(slider, 1)
                slider_row.addWidget(value_label)
                body.addLayout(slider_row)

            self._open_minimum_height = max(self._open_minimum_height, 166)

        root.addWidget(self._body)
        self._apply_expanded_state()

    def _toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._apply_expanded_state()
        if self._expanded_changed is not None:
            self._expanded_changed(bool(self._expanded))

    def _apply_expanded_state(self) -> None:
        if hasattr(self, "_body"):
            self._body.setVisible(bool(self._expanded))
        if self._collapse_button is not None:
            self._collapse_button.setText("▴" if self._expanded else "▾")
            self._collapse_button.setToolTip("Collapse" if self._expanded else "Expand")
        self.setMinimumHeight(self._open_minimum_height if self._expanded else 66)



def _add_balanced_dashboard_action_row(
    body_layout: QVBoxLayout,
    left_widget: QWidget,
    open_button: QPushButton,
    *,
    edge_width: int = 132,
) -> None:
    """Add the shared Pads/VFX balanced action row.

    Keeping this in one helper prevents the compact module dashboards from
    drifting apart as their controls evolve.
    """
    action_row = QHBoxLayout()
    action_row.setContentsMargins(0, 0, 0, 0)
    action_row.setSpacing(8)

    left_host = QWidget()
    left_host.setFixedWidth(int(edge_width))
    left_layout = QHBoxLayout(left_host)
    left_layout.setContentsMargins(0, 0, 16, 0)
    left_layout.setSpacing(0)
    left_layout.addStretch(1)
    left_layout.addWidget(left_widget)
    left_layout.addStretch(1)
    action_row.addWidget(left_host)

    action_row.addStretch(1)

    open_button.setFixedWidth(int(edge_width))
    action_row.addWidget(open_button)
    body_layout.addLayout(action_row)


class AppsPanel(QWidget):
    def __init__(self, backend_controller=None):
        super().__init__()
        self.backend_controller = backend_controller
        self._last_signature: tuple = ()
        self._permanent_routes_signature: tuple = ()
        self._micro_injection_expanded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(9)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        hint = QLabel("Active playback streams")
        hint.setObjectName("muted")
        top.addWidget(hint, 1)

        refresh_btn = QPushButton("↻")
        refresh_btn.setObjectName("padTopButton")
        refresh_btn.setToolTip("Refresh streams")
        refresh_btn.setFixedWidth(34)
        refresh_btn.clicked.connect(lambda: self.refresh_streams(force=True))
        top.addWidget(refresh_btn)

        root.addLayout(top)

        self.permanent_host = QWidget()
        self.permanent_layout = QVBoxLayout(self.permanent_host)
        self.permanent_layout.setContentsMargins(0, 0, 0, 0)
        self.permanent_layout.setSpacing(9)
        root.addWidget(self.permanent_host)

        self._build_permanent_routes()
        if self.backend_controller is not None and hasattr(self.backend_controller, "apply_glass_runtime_routes"):
            self.backend_controller.apply_glass_runtime_routes()

        self.streams_host = QWidget()
        self.streams_layout = QVBoxLayout(self.streams_host)
        self.streams_layout.setContentsMargins(0, 0, 0, 0)
        self.streams_layout.setSpacing(9)
        root.addWidget(self.streams_host)

        root.addStretch(1)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1400)
        self.refresh_timer.timeout.connect(self.refresh_streams)
        self.refresh_timer.start()

        self.refresh_streams(force=True)

    def _clear_permanent_routes(self) -> None:
        while self.permanent_layout.count():
            item = self.permanent_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _permanent_routes_current_signature(self) -> tuple:
        if self.backend_controller is None:
            return ()
        return_pairs = tuple(self.backend_controller.available_input_targets())
        return_source = ""
        if hasattr(self.backend_controller, "_return_mic_source_config"):
            return_source = str(self.backend_controller._return_mic_source_config() or "").strip()
        injected = tuple(sorted(self.backend_controller.micro_injection_channels()))
        return (
            return_pairs,
            return_source,
            self.backend_controller.processed_micro_source_label(),
            injected,
        )

    def _refresh_permanent_routes_if_needed(self, force: bool = False) -> None:
        try:
            signature = self._permanent_routes_current_signature()
        except Exception as exc:
            if self.backend_controller is not None:
                self.backend_controller.status_changed.emit(f"Device watcher error — {exc}")
            return
        if not force and signature == self._permanent_routes_signature:
            return
        self._permanent_routes_signature = signature
        self._build_permanent_routes()

    def _set_micro_injection_expanded(self, expanded: bool) -> None:
        self._micro_injection_expanded = bool(expanded)

    def _build_permanent_routes(self) -> None:
        self._clear_permanent_routes()

        if self.backend_controller is None:
            self.permanent_layout.addWidget(PermanentRouteCard("⚠", "Internal routes", "Backend not connected yet"))
            return

        return_pairs = self.backend_controller.available_input_targets()
        # The exported MICRO is a processed K-Sounds source, not a physical input.
        # Offer it explicitly here; available_input_targets() deliberately hides
        # internal sources when choosing the primary microphone capture device.
        return_inputs = ["Off", self.backend_controller.processed_micro_source_label()] + [
            label for label, _source in return_pairs
        ]
        current_return = self.backend_controller.return_mic_source_label()
        current_return_source = ""
        if hasattr(self.backend_controller, "_return_mic_source_config"):
            current_return_source = str(self.backend_controller._return_mic_source_config() or "").strip()
        if current_return_source and current_return_source != "micro" and not any(source == current_return_source for _label, source in return_pairs):
            current_return = _format_missing_device_label(current_return or current_return_source)
            if current_return not in return_inputs:
                return_inputs.insert(1, current_return)
        elif current_return not in return_inputs:
            current_return = "Off"

        self.permanent_layout.addWidget(
            PermanentRouteCard(
                "🎧",
                "MIC OUT / Return Mic",
                "Listen to a physical input or the processed K-Sounds MICRO in MIC OUT.",
                select_items=return_inputs,
                select_current=current_return,
                select_callback=self._set_return_mic_source,
            )
        )

        injected = self.backend_controller.micro_injection_channels()
        injection_checks = [
            ("ALL", "all" in injected, lambda value, key="all": self._set_micro_injection_channel(key, value)),
            ("GAME", "game" in injected, lambda value, key="game": self._set_micro_injection_channel(key, value)),
            ("CHAT", "chat" in injected, lambda value, key="chat": self._set_micro_injection_channel(key, value)),
            ("MEDIA", "media" in injected, lambda value, key="media": self._set_micro_injection_channel(key, value)),
            ("MORE", "more" in injected, lambda value, key="more": self._set_micro_injection_channel(key, value)),
        ]

        injection_volume = self.backend_controller.micro_injection_volume()

        self.permanent_layout.addWidget(
            PermanentRouteCard(
                "🎙",
                "MICRO Injection",
                "Send selected channels into the MICRO output.",
                checks=injection_checks,
                sliders=[
                    (
                        "Inject volume",
                        injection_volume,
                        0,
                        200,
                        self._set_micro_injection_volume,
                    ),
                ],
                collapsible=True,
                expanded=self._micro_injection_expanded,
                expanded_changed=self._set_micro_injection_expanded,
            )
        )

    def _set_soundboard_output(self, label: str) -> None:
        if self.backend_controller is None:
            return
        key = SOUNDBOARD_LOGICAL_KEY_BY_LABEL.get(str(label or "").strip())
        if not key:
            return
        self.backend_controller.set_soundboard_output_channel(key)
        self._build_permanent_routes()

    def _set_soundboard_to_mic_out(self, enabled: bool) -> None:
        if self.backend_controller is None:
            return
        self.backend_controller.set_soundboard_output_channel("return-mic" if enabled else "media")
        QTimer.singleShot(120, self._build_permanent_routes)

    def _set_soundboard_to_micro(self, enabled: bool) -> None:
        if self.backend_controller is None:
            return
        self.backend_controller.set_soundboard_micro_enabled(bool(enabled))
        QTimer.singleShot(80, self._build_permanent_routes)

    def _set_return_mic_source(self, label: str) -> None:
        if self.backend_controller is None:
            return
        selected = str(label or "").strip()
        if _is_missing_device_label(selected):
            self.backend_controller.status_changed.emit(f"Device not found: {_plain_missing_device_label(selected)}")
            return
        self.backend_controller.set_return_mic_source_label(selected)
        self._build_permanent_routes()

    def _set_return_mic_micro_source(self, enabled: bool) -> None:
        # Compatibility with older checkbox path. The UI now uses _set_return_mic_source().
        if self.backend_controller is None:
            return
        if enabled:
            self.backend_controller.set_return_mic_source_label("MICRO")
        else:
            self.backend_controller.set_return_mic_source_label("Off")
        self._build_permanent_routes()

    def _set_return_mic_output(self, label: str) -> None:
        if self.backend_controller is None:
            return
        sink = self.backend_controller.resolve_output_label(str(label or "").strip())
        if not sink:
            return
        self.backend_controller.set_channel_primary_target("return-mic", sink)

    def _set_micro_injection_channel(self, channel_key: str, enabled: bool) -> None:
        if self.backend_controller is None:
            return
        self.backend_controller.set_micro_injection_channel_state(channel_key, bool(enabled))
        self._build_permanent_routes()

    def _set_micro_injection_volume(self, volume: int) -> None:
        if self.backend_controller is None:
            return
        self.backend_controller.set_micro_injection_volume(int(volume))

    def _clear_streams(self) -> None:
        while self.streams_layout.count():
            item = self.streams_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _is_internal_soundboard_stream(self, stream) -> bool:
        sink_name = str(getattr(stream, "sink_name", "") or "").strip().lower()
        values = [
            getattr(stream, "display_name", ""),
            getattr(stream, "app_name", ""),
            getattr(stream, "binary_name", ""),
            getattr(stream, "media_name", ""),
            getattr(stream, "node_name", ""),
            sink_name,
        ]
        haystack = " ".join(str(value or "").lower() for value in values)

        internal_tokens = [
            "ksh_keepalive",
            "ksh_mic_physical",
            "k-sound-hub-soundboard-to-output",
            "k-sound-hub-soundboard-to-micro",
            "k-sound-hub-return-mic-micro",
            "k-sound-hub-micro-inject",
            "k-sounds hub mic output monitor",
            "k-sound hub all eq",
            "k-sound hub game eq",
            "k-sound hub chat eq",
            "k-sound hub media eq",
            "k-sound hub more eq",
            "k-sound hub return-mic eq",
            "k-sound-hub-soundboard",
            "k-sounds hub soundboard",
        ]

        if any(token in haystack for token in internal_tokens):
            return True
        if sink_name == "soundboard":
            return True
        if "soundboard" in haystack and any(token in haystack for token in ("pacat", "python", "k-sounds", "k-sound")):
            return True
        if "return-mic-micro" in haystack:
            return True
        return False

    def _stream_signature(self, streams: list) -> tuple:
        return tuple(
            (
                int(getattr(stream, "stream_id", -1)),
                str(getattr(stream, "display_name", "") or ""),
                str(getattr(stream, "sink_name", "") or ""),
                str(getattr(stream, "app_name", "") or ""),
                str(getattr(stream, "binary_name", "") or ""),
                str(getattr(stream, "media_name", "") or ""),
                str(getattr(stream, "node_name", "") or ""),
            )
            for stream in streams
        )

    def refresh_streams(self, force: bool = False) -> None:
        if self.backend_controller is None:
            self._show_message("Backend not connected yet")
            return

        self._refresh_permanent_routes_if_needed(force=force)

        streams = self.backend_controller.list_app_streams()
        visible_streams = [
            stream for stream in streams
            if not self._is_internal_soundboard_stream(stream)
        ]

        signature = self._stream_signature(visible_streams)
        if not force and signature == self._last_signature:
            return

        self._last_signature = signature
        self._clear_streams()

        if not visible_streams:
            self._show_message("No active external playback app stream")
            return

        for stream in visible_streams:
            self.streams_layout.addWidget(AppRouteCard(stream, self._move_stream))

    def _show_message(self, text: str) -> None:
        self._clear_streams()
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        self.streams_layout.addWidget(label)

    def _move_stream(self, stream_id: int, channel_key: str) -> bool:
        if self.backend_controller is None:
            return False

        ok = bool(self.backend_controller.move_app_stream(stream_id, channel_key))
        QTimer.singleShot(150, lambda: self.refresh_streams(force=True))
        return ok




class EqPanel(QWidget):
    CHANNELS = [
        ("ALL", "all"),
        ("GAME", "game"),
        ("CHAT", "chat"),
        ("MEDIA", "media"),
        ("MORE", "more"),
    ]

    def __init__(
        self,
        backend_controller=None,
        *,
        channels=None,
        show_channel_selector: bool = True,
        preset_shortcut_state_getter=None,
        preset_shortcut_change_callback=None,
    ):
        super().__init__()
        self.backend_controller = backend_controller
        self._channels = list(channels or self.CHANNELS)
        self._channel_label_by_key = {key: label for label, key in self._channels}
        self._channel_key_by_label = {label: key for label, key in self._channels}
        self._syncing = False
        self._pending_band_change: tuple[str, str, int, int] | None = None
        self._band_drag_active = False
        self._band_drag_history_before: dict | None = None
        self._pending_history_before: dict | None = None
        self._undo_stack: list[tuple[dict, dict, str]] = []
        self._redo_stack: list[tuple[dict, dict, str]] = []
        self._history_limit = 64
        self._micro_only = len(self._channels) == 1 and self._channels[0][1] == "micro"
        self._preset_shortcut_state_getter = preset_shortcut_state_getter if self._micro_only else None
        self._preset_shortcut_change_callback = preset_shortcut_change_callback if self._micro_only else None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        channel_labels = [label for label, _key in self._channels]
        default_label = channel_labels[0] if channel_labels else "GAME"
        self.channel_label = QLabel("Channel")
        self.eq_channel_select = SelectButton(
            channel_labels or [default_label],
            default_label,
            self._eq_channel_changed,
        )
        if show_channel_selector:
            root.addWidget(self.channel_label)
            root.addWidget(self.eq_channel_select)
        else:
            self.channel_label.hide()
            self.eq_channel_select.hide()

        root.addWidget(QLabel("Preset"))
        self.eq_preset_select = SelectButton(
            ["Default"],
            "Default",
            self._eq_preset_changed,
            favorite_getter=self._preset_is_favorite,
            favorite_callback=self._set_preset_favorite,
            favorite_other_label="Other presets",
        )
        root.addWidget(self.eq_preset_select)

        self.preset_shortcut_edit = None
        self.preset_shortcut_check = None
        if self._micro_only and callable(self._preset_shortcut_state_getter) and callable(self._preset_shortcut_change_callback):
            shortcut_row = QHBoxLayout()
            shortcut_row.setContentsMargins(0, 0, 0, 0)
            shortcut_row.setSpacing(6)
            shortcut_row.addWidget(QLabel("Preset shortcut"))
            self.preset_shortcut_edit = ShortcutKeyEdit(self)
            self.preset_shortcut_edit.setToolTip("Global shortcut for the selected MICRO preset. Enter validates, Delete clears, Esc cancels.")
            self.preset_shortcut_edit.shortcutCommitted.connect(self._micro_preset_shortcut_committed)
            shortcut_row.addWidget(self.preset_shortcut_edit, 1)
            self.preset_shortcut_check = QCheckBox("On")
            self.preset_shortcut_check.toggled.connect(self._micro_preset_shortcut_enabled_changed)
            shortcut_row.addWidget(self.preset_shortcut_check)
            root.addLayout(shortcut_row)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)

        self.new_button = QPushButton("New")
        self.new_button.setObjectName("padTopButton")
        self.new_button.clicked.connect(self._create_preset)
        actions.addWidget(self.new_button)

        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.setObjectName("padTopButton")
        self.duplicate_button.clicked.connect(self._duplicate_preset)
        actions.addWidget(self.duplicate_button)

        self.rename_button = QPushButton("Rename")
        self.rename_button.setObjectName("padTopButton")
        self.rename_button.clicked.connect(self._rename_preset)
        actions.addWidget(self.rename_button)

        self.delete_button = QPushButton("🗑")
        self.delete_button.setObjectName("padDeleteButton")
        self.delete_button.setToolTip("Delete EQ preset")
        self.delete_button.setFixedWidth(34)
        self.delete_button.clicked.connect(self._delete_preset)
        actions.addWidget(self.delete_button)

        actions.addStretch(1)

        self.undo_button = CenterGlyphButton("↶", y_offset=-1)
        self.undo_button.setObjectName("padTopButton")
        self.undo_button.setToolTip("Undo EQ change · Ctrl+Z")
        self.undo_button.setFixedWidth(34)
        self.undo_button.clicked.connect(self._undo_eq)
        actions.addWidget(self.undo_button)

        self.redo_button = CenterGlyphButton("↷", y_offset=-1)
        self.redo_button.setObjectName("padTopButton")
        self.redo_button.setToolTip("Redo EQ change · Ctrl+Y / Ctrl+Shift+Z")
        self.redo_button.setFixedWidth(34)
        self.redo_button.clicked.connect(self._redo_eq)
        actions.addWidget(self.redo_button)

        root.addLayout(actions)

        self.bands_card = QFrame()
        self.bands_card.setObjectName("channelCard")
        self.bands_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        bands_layout = QHBoxLayout(self.bands_card)
        bands_layout.setContentsMargins(7, 8, 7, 8)
        bands_layout.setSpacing(3)

        self.band_controls: list[tuple[QLabel, NoWheelSlider, QLabel]] = []
        for index in range(10):
            col = QVBoxLayout()
            col.setSpacing(3)

            gain_label = QLabel("+0.0")
            gain_label.setObjectName("muted")
            gain_label.setAlignment(Qt.AlignCenter)
            col.addWidget(gain_label)

            slider = NoWheelSlider(Qt.Vertical)
            slider.setRange(-24, 24)
            slider.setValue(0)
            slider.setMinimumHeight(118)
            slider.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            slider.setTracking(False)
            slider.sliderPressed.connect(lambda idx=index: self._eq_slider_pressed(idx))
            slider.sliderMoved.connect(lambda raw, idx=index: self._eq_band_moved(idx, raw))
            slider.sliderReleased.connect(lambda idx=index: self._eq_slider_released(idx))
            slider.valueChanged.connect(lambda raw, idx=index: self._eq_band_changed(idx, raw))
            col.addWidget(slider, 1, Qt.AlignHCenter)

            freq_label = QLabel("—")
            freq_label.setObjectName("muted")
            freq_label.setAlignment(Qt.AlignCenter)
            col.addWidget(freq_label)

            self.band_controls.append((gain_label, slider, freq_label))
            bands_layout.addLayout(col)

        root.addWidget(self.bands_card, 1)

        self.status_label = QLabel("EQ changes apply live and are saved automatically.")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.apply_timer = QTimer(self)
        self.apply_timer.setSingleShot(True)
        self.apply_timer.setInterval(90)
        self.apply_timer.timeout.connect(self._apply_pending_band)

        self.undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.undo_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.undo_shortcut.activated.connect(self._undo_eq)

        self.redo_shortcut = QShortcut(QKeySequence("Ctrl+Y"), self)
        self.redo_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.redo_shortcut.activated.connect(self._redo_eq)

        self.redo_alt_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self.redo_alt_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.redo_alt_shortcut.activated.connect(self._redo_eq)

        self._reload_profiles()
        self._update_history_actions()

    def _preset_is_favorite(self, name: str) -> bool:
        if self.backend_controller is None:
            return False
        if self._current_channel_key() == "micro":
            return bool(self.backend_controller.is_favorite_micro_eq_preset(name))
        return bool(self.backend_controller.is_favorite_output_eq_preset(name))

    def _set_preset_favorite(self, name: str, favorite: bool) -> bool:
        if self.backend_controller is None:
            return False
        if self._current_channel_key() == "micro":
            return bool(self.backend_controller.set_favorite_micro_eq_preset(name, favorite))
        return bool(self.backend_controller.set_favorite_output_eq_preset(name, favorite))

    def _sync_micro_preset_shortcut_ui(self) -> None:
        if self.preset_shortcut_edit is None or self.preset_shortcut_check is None:
            return
        getter = self._preset_shortcut_state_getter
        if not callable(getter):
            return
        trigger, enabled = getter(self._current_profile_name())
        self.preset_shortcut_edit.blockSignals(True)
        self.preset_shortcut_check.blockSignals(True)
        try:
            self.preset_shortcut_edit.setShortcutText(str(trigger or ""))
            self.preset_shortcut_check.setChecked(bool(enabled and str(trigger or "").strip()))
        finally:
            self.preset_shortcut_edit.blockSignals(False)
            self.preset_shortcut_check.blockSignals(False)

    def _micro_preset_shortcut_committed(self, trigger: str) -> None:
        callback = self._preset_shortcut_change_callback
        if not callable(callback):
            return
        callback(self._current_profile_name(), trigger=str(trigger or ""))
        self._sync_micro_preset_shortcut_ui()

    def _micro_preset_shortcut_enabled_changed(self, enabled: bool) -> None:
        callback = self._preset_shortcut_change_callback
        if not callable(callback):
            return
        callback(self._current_profile_name(), enabled=bool(enabled))
        self._sync_micro_preset_shortcut_ui()

    def select_channel(self, channel_key: str) -> None:
        key = str(channel_key or "").strip().lower()
        label = self._channel_label_by_key.get(key)
        if not label:
            return
        self._syncing = True
        self.eq_channel_select._current = label
        self.eq_channel_select._sync_text()
        self._syncing = False
        self._reload_profiles()

    def _current_channel_key(self) -> str:
        fallback = self._channels[0][1] if self._channels else "game"
        return self._channel_key_by_label.get(self.eq_channel_select.current_text(), fallback)

    def _current_profile_name(self) -> str:
        return str(self.eq_preset_select.current_text() or "").strip()

    def _set_select_items(self, select: SelectButton, items: list[str], current: str) -> None:
        select.items = list(items) if items else ["Default"]
        select._current = current if current in select.items else select.items[0]
        select._sync_text()

    def _ask_name(self, title: str, label: str, default: str) -> str:
        value, ok = QInputDialog.getText(self, title, label, text=str(default or ""))
        if not ok:
            return ""
        return str(value or "").strip()

    def _format_frequency(self, frequency: float) -> str:
        try:
            value = float(frequency)
        except Exception:
            return "—"
        if value >= 1000:
            return f"{value / 1000:g}k"
        return f"{int(value)}"

    def _format_gain(self, gain_db: float) -> str:
        try:
            value = round(float(gain_db) * 2.0) / 2.0
        except Exception:
            value = 0.0
        return f"{value:+.1f}"

    def _raw_to_gain(self, raw: int) -> float:
        return round(float(raw) / 2.0, 1)

    def _gain_to_raw(self, gain_db: float) -> int:
        try:
            return int(round(float(gain_db) * 2.0))
        except Exception:
            return 0

    def _reload_profiles(self, preferred: str | None = None) -> None:
        self._cancel_pending_band_change()
        if self.backend_controller is None:
            self.status_label.setText("EQ backend not connected yet.")
            return

        channel_key = self._current_channel_key()
        names, selected = self.backend_controller.eq_profile_names(channel_key)
        if preferred and preferred in names:
            selected = preferred

        self._syncing = True
        self._set_select_items(self.eq_preset_select, names or ["Default"], selected or (names[0] if names else "Default"))
        self._syncing = False

        self._reload_bands()
        self._sync_micro_preset_shortcut_ui()

    def _reload_bands(self) -> None:
        if self.backend_controller is None:
            return

        channel_key = self._current_channel_key()
        profile_name = self._current_profile_name()
        bands = self.backend_controller.eq_profile_bands(channel_key, profile_name)

        self._syncing = True
        for index, (gain_label, slider, freq_label) in enumerate(self.band_controls):
            if index < len(bands):
                frequency, gain_db, _q = bands[index]
                raw = self._gain_to_raw(gain_db)
                slider.setEnabled(True)
                slider.setValue(max(slider.minimum(), min(slider.maximum(), raw)))
                gain_label.setText(self._format_gain(self._raw_to_gain(slider.value())))
                freq_label.setText(self._format_frequency(frequency))
            else:
                slider.setEnabled(False)
                slider.setValue(0)
                gain_label.setText("—")
                freq_label.setText("—")
        self._syncing = False

        label = self._channel_label_by_key.get(channel_key, channel_key.upper())
        self.status_label.setText(f"{label} · {profile_name or 'Default'} · live EQ")

    def _eq_channel_changed(self, channel_label: str) -> None:
        if self._syncing:
            return
        self._reload_profiles()

    def _eq_preset_changed(self, preset: str) -> None:
        if self._syncing or self.backend_controller is None:
            return

        channel_key = self._current_channel_key()
        before = self._history_snapshot()
        if self.backend_controller.select_eq_profile(channel_key, preset):
            self._record_history(before, f"Select preset {preset}")
            self._reload_bands()
            self._sync_micro_preset_shortcut_ui()
        else:
            self.status_label.setText("Could not apply EQ preset.")

    def _create_preset(self) -> None:
        if self.backend_controller is None:
            return

        name = self._ask_name("New EQ preset", "Preset name:", "New preset")
        if not name:
            return

        before = self._history_snapshot()
        created = self.backend_controller.create_eq_profile(self._current_channel_key(), name)
        if created:
            self._record_history(before, f"Create preset {created}")
            self._reload_profiles(preferred=created)
            self.status_label.setText(f"Created preset: {created}")
        else:
            self.status_label.setText("Could not create EQ preset.")

    def _duplicate_preset(self) -> None:
        if self.backend_controller is None:
            return

        current = self._current_profile_name()
        name = self._ask_name("Duplicate EQ preset", "New preset name:", f"{current} copy")
        if not name:
            return

        before = self._history_snapshot()
        created = self.backend_controller.duplicate_eq_profile(self._current_channel_key(), current, name)
        if created:
            self._record_history(before, f"Duplicate preset {created}")
            self._reload_profiles(preferred=created)
            self.status_label.setText(f"Duplicated preset: {created}")
        else:
            self.status_label.setText("Could not duplicate EQ preset.")

    def _rename_preset(self) -> None:
        if self.backend_controller is None:
            return

        current = self._current_profile_name()
        name = self._ask_name("Rename EQ preset", "New preset name:", current)
        if not name:
            return

        before = self._history_snapshot()
        renamed = self.backend_controller.rename_eq_profile(self._current_channel_key(), current, name)
        if renamed:
            if self._micro_only and callable(self._preset_shortcut_state_getter) and callable(self._preset_shortcut_change_callback):
                old_trigger, old_enabled = self._preset_shortcut_state_getter(current)
                if str(old_trigger or "").strip():
                    self._preset_shortcut_change_callback(renamed, trigger=str(old_trigger or ""))
                    self._preset_shortcut_change_callback(renamed, enabled=bool(old_enabled))
                self._preset_shortcut_change_callback(current, enabled=False)
            self._record_history(before, f"Rename preset {current}")
            self._reload_profiles(preferred=renamed)
            self.status_label.setText(f"Renamed preset: {renamed}")
        else:
            self.status_label.setText("Could not rename EQ preset.")

    def _delete_preset(self) -> None:
        if self.backend_controller is None:
            return

        current = self._current_profile_name()
        names, _selected = self.backend_controller.eq_profile_names(self._current_channel_key())
        if len(names) <= 1:
            QMessageBox.information(self, "EQ preset", "At least one EQ preset must remain.")
            return

        answer = QMessageBox.question(
            self,
            "Delete EQ preset",
            f"Delete preset '{current}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        before = self._history_snapshot()
        if self._micro_only and callable(self._preset_shortcut_change_callback):
            self._preset_shortcut_change_callback(current, enabled=False)
        replacement = self.backend_controller.delete_eq_profile(self._current_channel_key(), current)
        if replacement:
            self._record_history(before, f"Delete preset {current}")
            self._reload_profiles(preferred=replacement)
            self.status_label.setText(f"Deleted preset: {current}")
        else:
            self.status_label.setText("Could not delete EQ preset.")

    def _preview_band_label(self, index: int, raw: int) -> None:
        if not (0 <= index < len(self.band_controls)):
            return
        gain_label, _slider, _freq_label = self.band_controls[index]
        gain_label.setText(self._format_gain(self._raw_to_gain(raw)))

    def _cancel_pending_band_change(self) -> None:
        timer = getattr(self, "apply_timer", None)
        if timer is not None:
            timer.stop()
        self._pending_band_change = None
        self._pending_history_before = None
        self._band_drag_history_before = None
        self._band_drag_active = False

    def _history_snapshot(self) -> dict | None:
        if self.backend_controller is None:
            return None
        try:
            return self.backend_controller.eq_history_snapshot(self._current_channel_key())
        except Exception:
            return None

    def _record_history(self, before: dict | None, label: str) -> None:
        if before is None:
            return
        after = self._history_snapshot()
        if after is None or before == after:
            return
        self._undo_stack.append((before, after, str(label or "EQ change")))
        if len(self._undo_stack) > self._history_limit:
            del self._undo_stack[:-self._history_limit]
        self._redo_stack.clear()
        self._update_history_actions()

    def _update_history_actions(self) -> None:
        if hasattr(self, "undo_button"):
            self.undo_button.setEnabled(bool(self._undo_stack))
            suffix = f" · {self._undo_stack[-1][2]}" if self._undo_stack else ""
            self.undo_button.setToolTip(f"Undo EQ change · Ctrl+Z{suffix}")
        if hasattr(self, "redo_button"):
            self.redo_button.setEnabled(bool(self._redo_stack))
            suffix = f" · {self._redo_stack[-1][2]}" if self._redo_stack else ""
            self.redo_button.setToolTip(f"Redo EQ change · Ctrl+Y / Ctrl+Shift+Z{suffix}")

    def _finish_pending_history(self) -> None:
        self.apply_timer.stop()
        if self._pending_band_change is not None:
            self._apply_pending_band()

    def _restore_history(self, snapshot: dict, label: str) -> bool:
        if self.backend_controller is None:
            return False
        ok = bool(
            self.backend_controller.restore_eq_history_snapshot(
                snapshot,
                self._current_channel_key(),
            )
        )
        if ok:
            self._reload_profiles()
            self.status_label.setText(label)
        return ok

    def _undo_eq(self) -> None:
        self._finish_pending_history()
        if not self._undo_stack:
            return
        item = self._undo_stack.pop()
        before, _after, label = item
        if self._restore_history(before, f"Undid: {label}"):
            self._redo_stack.append(item)
        else:
            self._undo_stack.append(item)
            self.status_label.setText("Could not undo EQ change.")
        self._update_history_actions()

    def _redo_eq(self) -> None:
        self._finish_pending_history()
        if not self._redo_stack:
            return
        item = self._redo_stack.pop()
        _before, after, label = item
        if self._restore_history(after, f"Redid: {label}"):
            self._undo_stack.append(item)
        else:
            self._redo_stack.append(item)
            self.status_label.setText("Could not redo EQ change.")
        self._update_history_actions()

    def _queue_band_change(self, index: int, raw: int) -> None:
        if not self._band_drag_active and self._pending_history_before is None:
            self._pending_history_before = self._history_snapshot()
        self._pending_band_change = (
            self._current_channel_key(),
            self._current_profile_name(),
            int(index),
            int(raw),
        )
        self.apply_timer.start()

    def _eq_slider_pressed(self, index: int) -> None:
        self._finish_pending_history()
        self._band_drag_active = True
        self._band_drag_history_before = self._history_snapshot()
        self._pending_history_before = None

    def _eq_slider_released(self, index: int) -> None:
        self.apply_timer.stop()
        self._apply_pending_band(record_history=False)
        self._band_drag_active = False
        before = self._band_drag_history_before
        self._band_drag_history_before = None
        self._pending_history_before = None
        self._record_history(before, f"Band {int(index) + 1}")

    def _eq_band_moved(self, index: int, raw: int) -> None:
        if self._syncing:
            return
        self._preview_band_label(index, raw)
        # A continuous gesture is coalesced: only the last value after a short
        # pause is committed, and release flushes it immediately.
        self._queue_band_change(index, raw)

    def _eq_band_changed(self, index: int, raw: int) -> None:
        if self._syncing:
            return
        self._preview_band_label(index, raw)
        self._queue_band_change(index, raw)
        if not self._band_drag_active:
            # Keyboard/programmatic changes still coalesce briefly. Mouse drag
            # release is handled above without the old extra 120 ms delay.
            self.apply_timer.start()

    def _apply_pending_band(self, record_history: bool = True) -> None:
        if self.backend_controller is None or self._pending_band_change is None:
            return

        channel_key, profile_name, index, raw = self._pending_band_change
        self._pending_band_change = None
        before = self._pending_history_before
        gain_db = self._raw_to_gain(raw)
        ok = self.backend_controller.set_eq_band_gain(
            channel_key,
            index,
            gain_db,
            profile_name,
        )
        if ok:
            self.status_label.setText(f"Saved band {index + 1}: {self._format_gain(gain_db)} dB")
            if record_history and not self._band_drag_active:
                self._record_history(before, f"Band {index + 1}")
        else:
            self.status_label.setText("Could not save EQ band.")
        if not self._band_drag_active:
            self._pending_history_before = None





class CenterGlyphButton(QPushButton):
    def __init__(self, glyph: str, parent=None, y_offset: int = 0):
        super().__init__("", parent)
        self._glyph = glyph
        self._y_offset = int(y_offset)
        self.setMinimumWidth(0)

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        if not self._glyph:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))

        metrics = painter.fontMetrics()
        bounds = metrics.tightBoundingRect(self._glyph)
        if bounds.isNull():
            bounds = metrics.boundingRect(self._glyph)

        x = (self.width() - bounds.width()) / 2.0 - bounds.left()
        y = (self.height() - bounds.height()) / 2.0 - bounds.top() + self._y_offset

        painter.drawText(int(round(x)), int(round(y)), self._glyph)

class HoverScrollLabel(QLabel):
    SCROLL_GAP = 34
    SCROLL_STEP = 2

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self._offset = 0
        self._scrolling = False

        self._timer = QTimer(self)
        self._timer.setInterval(35)
        self._timer.timeout.connect(self._tick_scroll)

        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text or "")
        self._offset = 0
        self.setToolTip(self._full_text)
        self.update()

    def text(self) -> str:
        return self._full_text

    def _needs_scroll(self) -> bool:
        width = max(8, self.contentsRect().width() - 2)
        return self.fontMetrics().horizontalAdvance(self._full_text) > width

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._scrolling = self._needs_scroll()
        self._offset = 0
        if self._scrolling:
            self._timer.start()
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._timer.stop()
        self._scrolling = False
        self._offset = 0
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._scrolling and not self._needs_scroll():
            self._timer.stop()
            self._scrolling = False
            self._offset = 0
        self.update()

    def _tick_scroll(self) -> None:
        text_width = self.fontMetrics().horizontalAdvance(self._full_text)
        limit = max(1, text_width + self.SCROLL_GAP)
        self._offset = (self._offset + self.SCROLL_STEP) % limit
        self.update()

    def paintEvent(self, event) -> None:
        if not self._full_text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))

        rect = self.contentsRect()
        metrics = self.fontMetrics()

        if not self._scrolling or not self._needs_scroll():
            width = max(8, rect.width() - 2)
            elided = metrics.elidedText(self._full_text, Qt.ElideRight, width)
            painter.drawText(rect, self.alignment() | Qt.TextSingleLine, elided)
            return

        text_width = metrics.horizontalAdvance(self._full_text)
        baseline = rect.y() + (rect.height() + metrics.ascent() - metrics.descent()) // 2
        x = rect.x() - self._offset

        painter.drawText(x, baseline, self._full_text)
        painter.drawText(x + text_width + self.SCROLL_GAP, baseline, self._full_text)


class SoundPadCard(QFrame):
    PAD_BG_DARKNESS = 62

    def __init__(
        self,
        name: str,
        icon: str,
        meta: str,
        emoji_callback=None,
        delete_callback=None,
        play_callback=None,
        slot_key: str = "",
        background_path: str = "",
        background_callback=None,
        sound_callback=None,
        trim_callback=None,
        folder_callback=None,
        shortcut_state_getter=None,
        shortcut_callback=None,
        trim_db: float = 0.0,
        folder_name: str = "Main",
    ):
        super().__init__()
        self._edit_enabled = False
        self._bulk_select_enabled = False
        self._bulk_selected = False
        self._emoji_callback = emoji_callback
        self._delete_callback = delete_callback
        self._play_callback = play_callback
        self._background_callback = background_callback
        self._sound_callback = sound_callback
        self._trim_callback = trim_callback
        self._folder_callback = folder_callback
        self._shortcut_state_getter = shortcut_state_getter
        self._shortcut_callback = shortcut_callback
        self._shortcut_popover = None
        self._shortcut_editor = None
        self._shortcut_check = None
        self._slot_key = str(slot_key or "").strip()
        self._folder_name = str(folder_name or "Main").strip() or "Main"
        self._always_show_meta = False
        self._background_path = str(background_path or "").strip()
        self._background_pixmap = QPixmap()
        self._auto_background_seed = self._slot_key or str(name or "sound")
        self._auto_background_palette = _soundboard_auto_palette(self._auto_background_seed)
        self._pressed_feedback = False

        self.setObjectName("soundPadCard")
        self.setProperty("edit", "false")
        self.setProperty("bulkSelected", "false")
        self.setProperty("missingSound", "false")
        self.setProperty("pressed", "false")
        self.setMinimumHeight(70)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(3)
        self._content_layout = root

        self._pressed_feedback_timer = QTimer(self)
        self._pressed_feedback_timer.setSingleShot(True)
        self._pressed_feedback_timer.setInterval(145)
        self._pressed_feedback_timer.timeout.connect(
            lambda: self._set_pressed_feedback(False)
        )

        self.icon_button = QPushButton(icon)
        self.icon_button.setObjectName("soundPadEmoji")
        self.icon_button.clicked.connect(self._request_emoji_palette)
        root.addWidget(self.icon_button, 0, Qt.AlignHCenter)

        self.name_label = HoverScrollLabel(name)
        self.name_label.setObjectName("soundPadName")
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setFixedHeight(17)
        self.name_label.mouseDoubleClickEvent = self._start_rename
        root.addWidget(self.name_label)

        self.name_editor = QLineEdit(name)
        self.name_editor.setObjectName("soundPadNameEditor")
        self.name_editor.setAlignment(Qt.AlignCenter)
        self.name_editor.setVisible(False)
        self.name_editor.editingFinished.connect(self._finish_rename)
        root.addWidget(self.name_editor)

        self.meta_label = HoverScrollLabel(meta)
        self.meta_label.setObjectName("soundPadMeta")
        self.meta_label.setAlignment(Qt.AlignCenter)
        self.meta_label.setFixedHeight(13)
        root.addWidget(self.meta_label)
        self.meta_label.setVisible(False)

        self.actions = QFrame()
        self.actions.setObjectName("soundPadActions")
        actions_outer = QVBoxLayout(self.actions)
        actions_outer.setContentsMargins(3, 2, 3, 3)
        actions_outer.setSpacing(3)

        actions_layout = QHBoxLayout()
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(3)
        actions_layout.addStretch(1)

        bg_button = QPushButton("🖼")
        bg_button.setObjectName("padIconButton")
        bg_button.setToolTip("Edit background")
        bg_button.clicked.connect(self._request_background)
        actions_layout.addWidget(bg_button)

        sound_button = QPushButton("🎵")
        sound_button.setObjectName("padIconButton")
        sound_button.setToolTip("Edit sound")
        sound_button.clicked.connect(self._request_sound)
        actions_layout.addWidget(sound_button)

        folder_button = QPushButton("📁")
        folder_button.setObjectName("padIconButton")
        folder_button.setToolTip("Move to folder")
        folder_button.clicked.connect(self._request_folder)
        actions_layout.addWidget(folder_button)

        self.shortcut_button = QPushButton("⌨")
        self.shortcut_button.setObjectName("padIconButton")
        self.shortcut_button.setToolTip("Edit global shortcut")
        self.shortcut_button.setEnabled(bool(self._slot_key))
        self.shortcut_button.clicked.connect(self._toggle_shortcut_popover)
        actions_layout.addWidget(self.shortcut_button)

        delete_button = CenterGlyphButton("🗑")
        delete_button.setObjectName("padDeleteButton")
        delete_button.setToolTip("Delete")
        delete_button.clicked.connect(self._request_delete)
        actions_layout.addWidget(delete_button)

        actions_layout.addStretch(1)
        actions_outer.addLayout(actions_layout)

        trim_layout = QHBoxLayout()
        trim_layout.setContentsMargins(0, 0, 0, 0)
        trim_layout.setSpacing(4)

        trim_down = QPushButton("−")
        trim_down.setObjectName("padIconButton")
        trim_down.setToolTip("Gain trim down 1 dB")
        trim_down.setMinimumHeight(24)
        trim_down.setMaximumHeight(24)
        trim_down.clicked.connect(lambda: self._change_trim_db(-1.0))
        trim_layout.addWidget(trim_down)

        self.trim_label = QLabel()
        self.trim_label.setObjectName("soundPadTrim")
        self.trim_label.setAlignment(Qt.AlignCenter)
        self.trim_label.setMinimumHeight(24)
        self.trim_label.setMaximumHeight(24)
        self.trim_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        trim_layout.addWidget(self.trim_label, 1)

        trim_up = QPushButton("+")
        trim_up.setObjectName("padIconButton")
        trim_up.setToolTip("Gain trim up 1 dB")
        trim_up.setMinimumHeight(24)
        trim_up.setMaximumHeight(24)
        trim_up.clicked.connect(lambda: self._change_trim_db(1.0))
        trim_layout.addWidget(trim_up)

        actions_outer.addLayout(trim_layout)

        self.actions.setVisible(False)
        root.addWidget(self.actions)
        self.set_trim_db(trim_db, emit=False)

    def mousePressEvent(self, event) -> None:
        if self._bulk_select_enabled and event.button() == Qt.LeftButton:
            self.set_bulk_selected(not self._bulk_selected)
            event.accept()
            return

        if event.button() == Qt.LeftButton and not self._edit_enabled:
            self._set_pressed_feedback(True)
            if self._request_play():
                event.accept()
                return
            self._set_pressed_feedback(False)

        super().mousePressEvent(event)

    def _set_pressed_feedback(self, pressed: bool) -> None:
        self._pressed_feedback = bool(pressed)
        self.setProperty("pressed", "true" if pressed else "false")
        layout = getattr(self, "_content_layout", None)
        if layout is not None:
            if pressed:
                layout.setContentsMargins(7, 7, 3, 3)
            else:
                layout.setContentsMargins(5, 5, 5, 5)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()
        if pressed:
            self._pressed_feedback_timer.start()

    def display_name(self) -> str:
        return self.name_label.text().strip() or "Unnamed"

    def set_emoji(self, emoji: str) -> None:
        self.icon_button.setText(emoji)

    def slot_key(self) -> str:
        return self._slot_key

    def set_slot_key(self, slot_key: str) -> None:
        self._slot_key = str(slot_key or "").strip()
        if hasattr(self, "shortcut_button"):
            self.shortcut_button.setEnabled(bool(self._slot_key))
        self._set_auto_background_seed(self._slot_key or self.display_name())

    def folder_name(self) -> str:
        return str(getattr(self, "_folder_name", "Main") or "Main")

    def set_folder_name(self, folder_name: str) -> None:
        self._folder_name = str(folder_name or "Main").strip() or "Main"

    def _set_auto_background_seed(self, seed: str) -> None:
        self._auto_background_seed = str(seed or "sound")
        self._auto_background_palette = _soundboard_auto_palette(self._auto_background_seed)
        if not getattr(self, "_background_pixmap", QPixmap()).isNull():
            return
        self.update()

    def set_missing_sound(self, missing: bool, path_text: str = "") -> None:
        missing = bool(missing)
        self.setProperty("missingSound", "true" if missing else "false")
        if missing:
            self.icon_button.setText("⚠")
            self.meta_label.setText("File missing")
            self.meta_label.setVisible(True)
            self.meta_label.setToolTip(str(path_text or "Missing sound file"))
            self.setToolTip(str(path_text or "Missing sound file"))
        else:
            self.setToolTip("")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_display_name(self, name: str) -> None:
        clean = str(name or "").strip() or "Unnamed"
        self.name_label.setText(clean)
        self.name_editor.setText(clean)
        if not self._slot_key:
            self._set_auto_background_seed(clean)

    def set_meta(self, meta: str) -> None:
        self.meta_label.setText(str(meta or ""))

    def set_always_show_meta(self, enabled: bool) -> None:
        self._always_show_meta = bool(enabled)
        if hasattr(self, "meta_label"):
            self.meta_label.setVisible(bool(self._edit_enabled or self._always_show_meta))

    def set_pad_background_darkness(self, value: int) -> None:
        try:
            numeric = int(value)
        except Exception:
            numeric = self.PAD_BG_DARKNESS
        self._pad_bg_darkness = max(0, min(100, numeric))
        if getattr(self, "_background_path", ""):
            self._apply_background_style()

    def set_background_path(self, path: str) -> None:
        self._background_path = str(path or "").strip()
        if not hasattr(self, "_pad_bg_darkness"):
            self._pad_bg_darkness = self.PAD_BG_DARKNESS
        self._apply_background_style()

    def _darkened_background_path(self, bg_path: Path) -> str:
        darkness = max(0, min(100, int(getattr(self, "_pad_bg_darkness", self.PAD_BG_DARKNESS))))
        if darkness <= 0:
            return str(bg_path)

        try:
            stat = bg_path.stat()
            key = hashlib.sha256(f"{bg_path}|{stat.st_mtime_ns}|{darkness}".encode("utf-8", "ignore")).hexdigest()[:24]
            cache_dir = CONFIG_DIR / "pad-bg-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{key}.png"
            if cached.is_file():
                return str(cached)

            source = QPixmap(str(bg_path))
            if source.isNull():
                return str(bg_path)

            size = QSize(640, 640)
            scaled = source.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            result = QPixmap(size)
            result.fill(Qt.transparent)

            painter = QPainter(result)
            x = (size.width() - scaled.width()) // 2
            y = (size.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            alpha = int(max(0, min(215, darkness * 2.1)))
            painter.setOpacity(alpha / 255.0)
            painter.fillRect(result.rect(), Qt.GlobalColor.black)
            painter.end()

            if result.save(str(cached), "PNG"):
                return str(cached)
        except Exception:
            return str(bg_path)

        return str(bg_path)

    def _apply_background_style(self) -> None:
        bg_path = Path(getattr(self, "_background_path", "") or "").expanduser()
        if not bg_path.is_file():
            self._background_pixmap = QPixmap()
            self.setStyleSheet("")
            self.update()
            return

        pixmap = QPixmap(self._darkened_background_path(bg_path))
        self._background_pixmap = pixmap if not pixmap.isNull() else QPixmap()
        self.setStyleSheet("""
QFrame#soundPadCard {
    border-radius: 16px;
}
QFrame#soundPadCard QLabel#soundPadName,
QFrame#soundPadCard QLabel#soundPadMeta,
QFrame#soundPadCard QLabel#soundPadTrim {
    background: rgba(0, 0, 0, 178);
    border-radius: 8px;
    padding: 2px 6px;
}
QFrame#soundPadCard QPushButton#soundPadEmoji {
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    background: rgba(0, 0, 0, 230);
    border-radius: 17px;
}
""")
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        pixmap = getattr(self, "_background_pixmap", QPixmap())
        rect = self.rect()
        if rect.width() <= 1 or rect.height() <= 1:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 16, 16)
        painter.setClipPath(path)

        if not pixmap.isNull():
            scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            first, second = getattr(
                self,
                "_auto_background_palette",
                SOUNDBOARD_AUTO_PALETTES[0],
            )
            gradient = QLinearGradient(0, 0, rect.width(), rect.height())
            gradient.setColorAt(0.0, QColor(*first, 218))
            gradient.setColorAt(1.0, QColor(*second, 218))
            painter.fillPath(path, gradient)

            # A subtle deterministic highlight gives empty pads an intentional
            # visual identity while remaining cheaper than decoding an image.
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 18))
            diameter = max(34, min(rect.width(), rect.height()) // 2)
            painter.drawEllipse(rect.width() - diameter // 2, -diameter // 3, diameter, diameter)

        if bool(getattr(self, "_pressed_feedback", False)):
            painter.fillPath(path, QColor(42, 190, 225, 64))
        painter.end()

    def _request_background(self) -> None:
        if self._edit_enabled and self._background_callback is not None:
            self._background_callback(self)

    def _request_sound(self) -> None:
        if self._edit_enabled and self._sound_callback is not None:
            self._sound_callback(self)

    def _request_folder(self) -> None:
        if self._edit_enabled and self._folder_callback is not None:
            self._folder_callback(self)

    def _current_shortcut_state(self) -> tuple[str, bool]:
        getter = self._shortcut_state_getter
        if not callable(getter) or not self._slot_key:
            return "", False
        try:
            trigger, enabled = getter(self._slot_key)
        except Exception:
            return "", False
        clean = str(trigger or "").strip()
        return clean, bool(enabled and clean)

    def sync_shortcut_binding(self, trigger: str, enabled: bool) -> None:
        editor = getattr(self, "_shortcut_editor", None)
        check = getattr(self, "_shortcut_check", None)
        clean = str(trigger or "").strip()
        if editor is not None:
            editor.setShortcutText(clean)
        if check is not None:
            check.blockSignals(True)
            check.setEnabled(bool(clean))
            check.setChecked(bool(enabled and clean))
            check.blockSignals(False)

    def _ensure_shortcut_popover(self) -> QFrame:
        parent = self.window() or self
        popover = getattr(self, "_shortcut_popover", None)
        if popover is not None:
            return popover

        popover = QFrame(parent)
        popover.setObjectName("padShortcutPopover")
        popover.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        popover.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(popover)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        title = QLabel("Pad shortcut", popover)
        title.setObjectName("padShortcutTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        name = QLabel(self.display_name(), popover)
        name.setObjectName("padShortcutName")
        name.setAlignment(Qt.AlignCenter)
        name.setWordWrap(False)
        layout.addWidget(name)
        self._shortcut_name_label = name

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        editor = ShortcutKeyEdit(popover)
        editor.setObjectName("padShortcutEdit")
        editor.setFixedHeight(28)
        editor.setMinimumWidth(150)
        editor.setToolTip("Press one global shortcut. Backspace/Delete clears; Enter validates; Esc cancels.")
        editor.shortcutCommitted.connect(self._on_pad_shortcut_trigger_changed)
        row.addWidget(editor, 1)

        check = QCheckBox(popover)
        check.setObjectName("padShortcutCheck")
        check.setToolTip("Enable this pad shortcut")
        check.toggled.connect(self._on_pad_shortcut_enabled_changed)
        row.addWidget(check, 0, Qt.AlignCenter)
        layout.addLayout(row)

        popover.setStyleSheet("""
QFrame#padShortcutPopover {
    background: rgba(5, 8, 14, 242);
    border: 1px solid rgba(110, 190, 230, 72);
    border-radius: 13px;
}
QLabel#padShortcutTitle {
    color: rgba(245, 250, 255, 250);
    font-size: 12px;
    font-weight: 900;
}
QLabel#padShortcutName {
    color: rgba(190, 215, 232, 220);
    font-size: 10px;
    font-weight: 700;
}
QKeySequenceEdit#padShortcutEdit {
    color: rgba(245, 250, 255, 248);
    background: rgba(255, 255, 255, 34);
    border: 1px solid rgba(190, 225, 245, 90);
    border-radius: 7px;
    padding: 2px 7px;
}
QKeySequenceEdit#padShortcutEdit:focus {
    background: rgba(35, 115, 155, 92);
    border: 1px solid rgba(110, 215, 255, 205);
}
QCheckBox#padShortcutCheck::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid rgba(190, 225, 245, 105);
    background: rgba(255, 255, 255, 28);
}
QCheckBox#padShortcutCheck::indicator:checked {
    background: rgba(92, 204, 255, 220);
    border: 1px solid rgba(160, 230, 255, 245);
}
""")

        self._shortcut_popover = popover
        self._shortcut_editor = editor
        self._shortcut_check = check
        popover.setFixedSize(238, 104)
        return popover

    def _toggle_shortcut_popover(self) -> None:
        if not self._edit_enabled or not self._slot_key:
            return
        popover = self._ensure_shortcut_popover()
        if popover.isVisible():
            popover.hide()
            return
        if hasattr(self, "_shortcut_name_label"):
            self._shortcut_name_label.setText(self.display_name())
        trigger, enabled = self._current_shortcut_state()
        self.sync_shortcut_binding(trigger, enabled)
        anchor = self.shortcut_button.mapToGlobal(QPoint(0, self.shortcut_button.height() + 4))
        screen = QApplication.screenAt(anchor)
        if screen is not None:
            bounds = screen.availableGeometry()
            x = min(anchor.x(), bounds.right() - popover.width())
            y = min(anchor.y(), bounds.bottom() - popover.height())
            anchor = QPoint(max(bounds.left(), x), max(bounds.top(), y))
        popover.move(anchor)
        popover.show()
        popover.raise_()

    def _on_pad_shortcut_trigger_changed(self, trigger: str) -> None:
        callback = self._shortcut_callback
        if not callable(callback) or not self._slot_key:
            return
        clean = str(trigger or "").strip()
        try:
            callback(self._slot_key, trigger=clean, enabled=bool(clean))
        except Exception:
            return
        check = getattr(self, "_shortcut_check", None)
        if check is not None:
            check.blockSignals(True)
            check.setEnabled(bool(clean))
            check.setChecked(bool(clean))
            check.blockSignals(False)

    def _on_pad_shortcut_enabled_changed(self, enabled: bool) -> None:
        callback = self._shortcut_callback
        if not callable(callback) or not self._slot_key:
            return
        try:
            callback(self._slot_key, enabled=bool(enabled))
        except Exception:
            pass

    def trim_db(self) -> float:
        return float(getattr(self, "_trim_db", 0.0))

    def set_trim_db(self, value: float, emit: bool = False) -> None:
        try:
            numeric = float(value)
        except Exception:
            numeric = 0.0
        self._trim_db = max(-24.0, min(24.0, numeric))
        if hasattr(self, "trim_label"):
            if abs(self._trim_db) < 0.05:
                text = "0 dB"
            else:
                text = f"{self._trim_db:+.0f} dB"
            self.trim_label.setText(text)
            self.trim_label.setToolTip(f"Manual gain trim: {text}")
        if emit and self._trim_callback is not None:
            self._trim_callback(self, self._trim_db)

    def _change_trim_db(self, delta: float) -> None:
        self.set_trim_db(self.trim_db() + float(delta), emit=True)

    def set_bulk_select_enabled(self, enabled: bool) -> None:
        self._bulk_select_enabled = bool(enabled)

        if enabled:
            self.actions.setVisible(False)
            self.setProperty("edit", "false")
            self.setMinimumHeight(70)
            if hasattr(self, "meta_label"):
                self.meta_label.setVisible(False)
        else:
            self.set_bulk_selected(False)
            self.actions.setVisible(self._edit_enabled)
            self.setProperty("edit", "true" if self._edit_enabled else "false")
            self.setMinimumHeight(100 if self._edit_enabled else 70)
            if hasattr(self, "meta_label"):
                self.meta_label.setVisible(bool(self._edit_enabled or getattr(self, "_always_show_meta", False)))

        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_bulk_selected(self, selected: bool) -> None:
        self._bulk_selected = selected
        self.setProperty("bulkSelected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def is_bulk_selected(self) -> bool:
        return self._bulk_selected

    def _request_delete(self) -> None:
        if self._delete_callback is not None:
            self._delete_callback(self)

    def _request_play(self) -> bool:
        if self._edit_enabled or self._bulk_select_enabled:
            return False
        if not self._slot_key or self._play_callback is None:
            return False
        callback = self._play_callback
        slot_key = self._slot_key
        QTimer.singleShot(0, lambda: callback(slot_key))
        return True

    def _request_emoji_palette(self) -> None:
        if not self._edit_enabled:
            return
        if self._emoji_callback is not None:
            self._emoji_callback(self)

    def _start_rename(self, event) -> None:
        if not self._edit_enabled:
            return
        self.name_editor.setText(self.name_label.text())
        self.name_label.setVisible(False)
        self.name_editor.setVisible(True)
        self.name_editor.setFocus()
        self.name_editor.selectAll()

    def _finish_rename(self) -> None:
        new_name = self.name_editor.text().strip() or "Unnamed"
        self.name_label.setText(new_name)
        self.name_editor.setVisible(False)
        self.name_label.setVisible(True)

    def set_edit_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._edit_enabled == enabled and self.actions.isVisible() == enabled:
            return

        self._edit_enabled = enabled
        self.setProperty("edit", "true" if enabled else "false")
        self.actions.setVisible(enabled)
        self.setMinimumHeight(100 if enabled else 70)

        if hasattr(self, "meta_label"):
            self.meta_label.setVisible(bool(enabled or getattr(self, "_always_show_meta", False)))

        if not enabled:
            self.name_editor.setVisible(False)
            self.name_label.setVisible(True)
            popover = getattr(self, "_shortcut_popover", None)
            if popover is not None:
                popover.hide()
            if self._bulk_select_enabled:
                self.set_bulk_select_enabled(False)

        self.style().unpolish(self)
        self.style().polish(self)
        self.update()



class AndroidPairOverlayMixin:
    """One shared in-app Android pairing popup for Pads and Voice FX."""

    def _pair_overlay_parent(self):
        try:
            parent = self.window()
            if parent is not None:
                return parent
        except Exception:
            pass
        return self

    def _ensure_pair_overlay(self) -> QFrame:
        parent = self._pair_overlay_parent()
        overlay = getattr(self, "pair_overlay", None)
        if overlay is not None and overlay.parent() is parent:
            return overlay

        if overlay is not None:
            try:
                overlay.hide()
                overlay.deleteLater()
            except Exception:
                pass

        overlay = QFrame(parent)
        overlay.setObjectName("pairOverlayDim")
        overlay.setAttribute(Qt.WA_StyledBackground, True)

        root = QVBoxLayout(overlay)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)
        root.addStretch(1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)

        card = QFrame(overlay)
        card.setObjectName("pairOverlayCard")
        card.setMaximumWidth(420)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Android pairing code")
        title.setObjectName("pairOverlayTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        code = QLineEdit("")
        code.setObjectName("pairOverlayCode")
        code.setReadOnly(True)
        code.setAlignment(Qt.AlignCenter)
        code.setMinimumHeight(46)
        layout.addWidget(code)
        self.pair_code_input = code

        hint = QLabel(
            "Open K-Sounds Remote on Android, search the PC, then enter this code.\n"
            "The code expires in 5 minutes."
        )
        hint.setObjectName("pairOverlayHint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        copy_button = QPushButton("Copy")
        copy_button.setObjectName("pairOverlayButton")
        copy_button.clicked.connect(self._copy_pair_code)
        buttons.addWidget(copy_button)

        close_button = QPushButton("Close")
        close_button.setObjectName("pairOverlayButton")
        close_button.clicked.connect(self._hide_pair_overlay)
        buttons.addWidget(close_button)

        layout.addLayout(buttons)

        row.addWidget(card)
        row.addStretch(1)

        root.addLayout(row)
        root.addStretch(1)

        overlay.setStyleSheet("""
QFrame#pairOverlayDim {
    background: rgba(0, 0, 0, 150);
}
QFrame#pairOverlayCard {
    background: rgba(7, 10, 18, 245);
    border: 1px solid rgba(62, 216, 255, 95);
    border-radius: 18px;
}
QLabel#pairOverlayTitle {
    color: rgba(236, 247, 255, 245);
    font-size: 16px;
    font-weight: 800;
}
QLineEdit#pairOverlayCode {
    color: white;
    background: rgba(0, 0, 0, 190);
    border: 1px solid rgba(62, 216, 255, 120);
    border-radius: 12px;
    font-size: 26px;
    font-weight: 900;
    letter-spacing: 5px;
}
QLabel#pairOverlayHint {
    color: rgba(198, 218, 232, 220);
    font-size: 12px;
}
QPushButton#pairOverlayButton {
    color: white;
    background: rgba(22, 42, 70, 220);
    border: 1px solid rgba(62, 216, 255, 90);
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 700;
}
QPushButton#pairOverlayButton:hover {
    background: rgba(38, 78, 118, 235);
}
""")

        self.pair_overlay = overlay
        self._position_pair_overlay()
        return overlay

    def _position_pair_overlay(self) -> None:
        overlay = getattr(self, "pair_overlay", None)
        if overlay is None:
            return

        parent = overlay.parentWidget()
        if parent is None:
            return

        try:
            overlay.setGeometry(parent.rect())
        except Exception:
            overlay.setGeometry(self.rect())

    def _show_pair_overlay(self, pin: str) -> None:
        overlay = self._ensure_pair_overlay()
        self._position_pair_overlay()

        code = getattr(self, "pair_code_input", None)
        if code is not None:
            code.setText(str(pin or ""))
            code.selectAll()
            code.setFocus()

        overlay.show()
        overlay.raise_()

    def _hide_pair_overlay(self) -> None:
        overlay = getattr(self, "pair_overlay", None)
        if overlay is not None:
            overlay.hide()

    def _copy_pair_code(self) -> None:
        code = getattr(self, "pair_code_input", None)
        if code is None:
            return
        try:
            QApplication.clipboard().setText(code.text())
        except Exception:
            pass



class PadsPanel(AndroidPairOverlayMixin, QWidget):
    EMOJIS = [
        "😀", "😃", "😄", "😁", "😆", "😂", "🤣", "🙂", "😉", "😊", "😎", "🤔",
        "😐", "😮", "😱", "🥳", "😈", "🤖", "👻", "💀", "👋", "👏", "👍", "👎",
        "❤️", "💙", "💚", "⭐", "✨", "🔥", "⚡", "💥", "✅", "❌", "⚠️", "🚨",
        "🎵", "🎶", "🎧", "🎤", "📣", "🔔", "🎬", "🎮", "🖱", "⌨️", "🏆", "🎯",
        "🐱", "🐶", "🐸", "🦊", "🐺", "🐲", "🍕", "☕", "🍺", "🚀", "🛸", "🌌",
        "🔊", "🔇", "🔈", "🔉", "📢", "🎺", "🥁", "🎹", "🎸", "🎻", "🎲", "🧨",
        "🟢", "🔴", "🟡", "🔵", "🟣", "🟠", "⬆️", "⬇️", "➡️", "⬅️", "💤", "💫",
    ]

    SAMPLE_PADS = [
        ("Airhorn", "📣", "00:02"),
        ("Click", "🖱", "00:01"),
        ("Bruh", "😐", "00:02"),
        ("Laugh", "😂", "00:03"),
        ("Alert", "🚨", "00:02"),
        ("GG", "🏆", "00:01"),
        ("Intro", "🎬", "00:05"),
        ("Drop", "💥", "00:03"),
    ]

    def _read_pad_bg_darkness_setting(self) -> int:
        settings_path = CONFIG_DIR / "settings.json"
        try:
            if settings_path.is_file():
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return max(0, min(100, int(data.get("glass_pad_bg_darkness", 62))))
        except Exception:
            pass
        return 62

    def _save_pad_bg_darkness_setting(self, value: int) -> None:
        settings_path = CONFIG_DIR / "settings.json"
        try:
            data = {}
            if settings_path.is_file():
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            data["glass_pad_bg_darkness"] = int(max(0, min(100, value)))
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _set_pad_bg_darkness(self, value: int, persist: bool = True) -> None:
        try:
            numeric = int(value)
        except Exception:
            numeric = 62
        self._pad_bg_darkness = max(0, min(100, numeric))
        if persist:
            self._save_pad_bg_darkness_setting(self._pad_bg_darkness)

        host = getattr(self, "grid_host", None)
        scroll = getattr(self, "grid_scroll", None)
        if host is not None:
            host.setUpdatesEnabled(False)
        if scroll is not None:
            scroll.setUpdatesEnabled(False)
        try:
            for card in getattr(self, "pad_cards", []):
                setter = getattr(card, "set_pad_background_darkness", None)
                if callable(setter):
                    setter(self._pad_bg_darkness)
        finally:
            if host is not None:
                host.setUpdatesEnabled(True)
            if scroll is not None:
                scroll.setUpdatesEnabled(True)

    def _queue_pad_bg_darkness(self, value: int) -> None:
        self._pending_pad_bg_darkness = int(value)
        self._pad_bg_darkness_timer.start()

    def _apply_queued_pad_bg_darkness(self) -> None:
        self._set_pad_bg_darkness(int(getattr(self, "_pending_pad_bg_darkness", self._pad_bg_darkness)))

    def _apply_responsive_card_heights(self) -> None:
        if bool(getattr(self, "_show_detach", True)):
            return
        if not hasattr(self, "grid_scroll") or not hasattr(self, "grid"):
            return

        columns = max(1, int(getattr(self, "_columns", 1)))
        viewport_width = max(1, self.grid_scroll.viewport().width())
        spacing = max(0, int(self.grid.horizontalSpacing()))
        card_width = max(72, (viewport_width - spacing * max(0, columns - 1)) // columns)
        target_height = max(116, min(176, int(card_width * 0.92)))

        for card in getattr(self, "pad_cards", []):
            if card.minimumHeight() != target_height:
                card.setMinimumHeight(target_height)

    def _update_responsive_columns(self) -> None:
        if not hasattr(self, "grid_scroll"):
            return

        if bool(getattr(self, "_show_detach", True)):
            return

        width = max(1, self.grid_scroll.viewport().width())
        spacing = 4 if width < 900 else 5
        if self.grid.horizontalSpacing() != spacing:
            self.grid.setHorizontalSpacing(spacing)
            self.grid.setVerticalSpacing(spacing)

        min_card_width = 118 if width >= 900 else 132
        columns = max(1, min(12, (width + spacing) // max(1, min_card_width + spacing)))

        try:
            self.grid_host.setMinimumWidth(width)
        except Exception:
            pass

        if columns != self._columns:
            self._columns = columns
            self._reflow_grid()
        else:
            self._apply_responsive_card_heights()

    def _read_soundboard_volume_setting(self) -> int:
        try:
            return max(0, min(100, int(self.soundboard_service.read_global_volume())))
        except Exception:
            pass
        return 100

    def _read_soundboard_auto_level_enabled(self) -> bool:
        try:
            return bool(self.soundboard_service.read_auto_level_enabled())
        except Exception:
            pass
        return False

    def _save_soundboard_auto_level_enabled(self, enabled: bool) -> None:
        try:
            self.soundboard_service.set_auto_level_enabled(bool(enabled))
        except Exception:
            pass

    def _save_soundboard_volume_setting(self, value: int) -> None:
        try:
            self.soundboard_service.set_global_volume(value)
        except Exception:
            pass

    def _set_soundboard_volume(self, value: int, persist: bool = True) -> None:
        try:
            numeric = int(value)
        except Exception:
            numeric = 100

        self._soundboard_volume = max(0, min(100, numeric))
        self._pending_soundboard_volume = self._soundboard_volume

        slider = getattr(self, "soundboard_volume_slider", None)
        if slider is not None and slider.value() != self._soundboard_volume:
            old = slider.blockSignals(True)
            try:
                slider.setValue(self._soundboard_volume)
            finally:
                slider.blockSignals(old)

        value_label = getattr(self, "soundboard_volume_value", None)
        if value_label is not None:
            value_label.setText(f"{self._soundboard_volume}%")

        if persist:
            self._save_soundboard_volume_setting(self._soundboard_volume)

            # Dashboard and detached Soundboard are separate PadsPanel instances.
            for widget in QApplication.allWidgets():
                if isinstance(widget, PadsPanel) and widget is not self:
                    widget._set_soundboard_volume(
                        self._soundboard_volume,
                        persist=False,
                    )

    def _set_soundboard_auto_level_enabled(self, enabled: bool, persist: bool = True) -> None:
        self._soundboard_auto_level_enabled = bool(enabled)

        check = getattr(self, "soundboard_auto_level_check", None)
        if check is not None:
            check.setText("Auto-level")
            check.setToolTip(
                "Auto-level is enabled. Manual dB trim still applies."
                if self._soundboard_auto_level_enabled
                else "Auto-level is disabled. Only manual dB trim applies."
            )
            if check.isChecked() != self._soundboard_auto_level_enabled:
                old = check.blockSignals(True)
                try:
                    check.setChecked(self._soundboard_auto_level_enabled)
                finally:
                    check.blockSignals(old)

        if persist:
            self._save_soundboard_auto_level_enabled(self._soundboard_auto_level_enabled)

            for widget in QApplication.allWidgets():
                if isinstance(widget, PadsPanel) and widget is not self:
                    widget._set_soundboard_auto_level_enabled(
                        self._soundboard_auto_level_enabled,
                        persist=False,
                    )

    def _analyze_soundboard_auto_level(self) -> None:
        if bool(getattr(self, "_soundboard_auto_level_analyzing", False)):
            return

        self._soundboard_auto_level_analyzing = True
        result_queue = queue.Queue(maxsize=1)
        self._soundboard_auto_level_result_queue = result_queue

        button = getattr(self, "soundboard_analyze_button", None)
        if button is not None:
            button.setEnabled(False)
            button.setText("…")

        worker = threading.Thread(
            target=self._analyze_soundboard_auto_level_worker,
            args=(result_queue,),
            name="KSoundsAutoLevelAnalyze",
            daemon=True,
        )
        self._soundboard_auto_level_worker = worker
        worker.start()
        QTimer.singleShot(150, self._poll_soundboard_auto_level_analysis)

    def _analyze_soundboard_auto_level_worker(self, result_queue) -> None:
        try:
            result = self._analyze_soundboard_auto_level_now()
        except Exception as exc:
            result = {"ok": False, "message": f"Analyze failed: {exc}"}

        try:
            result_queue.put_nowait(result)
        except Exception:
            pass

    def _poll_soundboard_auto_level_analysis(self) -> None:
        result_queue = getattr(self, "_soundboard_auto_level_result_queue", None)
        if result_queue is None:
            return

        try:
            result = result_queue.get_nowait()
        except queue.Empty:
            if bool(getattr(self, "_soundboard_auto_level_analyzing", False)):
                QTimer.singleShot(150, self._poll_soundboard_auto_level_analysis)
            return

        self._soundboard_auto_level_analyzing = False

        button = getattr(self, "soundboard_analyze_button", None)
        if button is not None:
            button.setEnabled(True)
            button.setText("↻")

        message = str(result.get("message") or "Analyze finished.")

        # Refresh every visible Soundboard instance after analysis.
        for widget in QApplication.allWidgets():
            if not isinstance(widget, PadsPanel):
                continue
            widget._set_soundboard_auto_level_enabled(
                widget._read_soundboard_auto_level_enabled(),
                persist=False,
            )
            widget._reload_soundboard_pads()

        if bool(result.get("ok")):
            QMessageBox.information(self, "Analyze Auto-level", message)
        else:
            QMessageBox.warning(self, "Analyze Auto-level", message)

    def _analyze_soundboard_auto_level_now(self) -> dict:
        if not SOUNDBOARD_PATH.is_file():
            return {"ok": False, "message": "soundboard.json not found."}

        raw_bytes = SOUNDBOARD_PATH.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))
        slots = data.get("slots", [])
        if not isinstance(slots, list):
            return {"ok": False, "message": "soundboard.json has no valid slots list."}

        backup_path = SOUNDBOARD_PATH.with_name(
            f"soundboard.json.backup-before-analyze-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        backup_path.write_bytes(raw_bytes)

        target_peak_db = -3.0
        min_gain = 0.05
        max_gain = 1.5

        changed = 0
        present = 0
        missing = 0
        failed = 0
        values: list[float] = []

        for slot in slots:
            if not isinstance(slot, dict):
                continue

            path_text = str(slot.get("path") or "").strip()
            if not path_text:
                continue

            sound_path = Path(path_text).expanduser()
            if not sound_path.is_file():
                missing += 1
                continue

            present += 1

            try:
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-nostats",
                        "-i",
                        str(sound_path),
                        "-filter:a",
                        "volumedetect",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except FileNotFoundError:
                return {
                    "ok": False,
                    "message": "ffmpeg was not found. Auto-level analysis could not run.",
                }
            except Exception:
                failed += 1
                continue

            output = f"{proc.stdout}\n{proc.stderr}"
            match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", output)
            if not match:
                failed += 1
                continue

            try:
                max_db = float(match.group(1))
            except Exception:
                failed += 1
                continue

            gain = 10 ** ((target_peak_db - max_db) / 20.0)
            gain = round(max(min_gain, min(max_gain, gain)), 4)

            if slot.get("auto_gain") != gain or str(slot.get("analyzed_path") or "") != str(sound_path):
                slot["auto_gain"] = gain
                slot["analyzed_path"] = str(sound_path)
                changed += 1

            values.append(gain)

        SOUNDBOARD_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        if values:
            values_sorted = sorted(values)
            median = values_sorted[len(values_sorted) // 2]
            message = (
                f"Analyzed {present} sound(s).\n"
                f"Changed: {changed} · Missing: {missing} · Failed: {failed}\n"
                f"Gain min/median/max: {min(values):.4g} / {median:.4g} / {max(values):.4g}\n"
                f"> 1.0: {sum(1 for value in values if value > 1.0)} · capped at 1.5: {sum(1 for value in values if abs(value - 1.5) < 0.0001)}\n"
                f"Backup: {backup_path}"
            )
        else:
            message = (
                f"No present sound files were analyzed.\n"
                f"Missing: {missing} · Failed: {failed}\n"
                f"Backup: {backup_path}"
            )

        return {"ok": failed == 0, "message": message}

    def _queue_soundboard_volume(self, value: int) -> None:
        self._pending_soundboard_volume = int(value)
        self._soundboard_volume_timer.start()

    def _apply_queued_soundboard_volume(self) -> None:
        self._set_soundboard_volume(int(getattr(self, "_pending_soundboard_volume", self._soundboard_volume)))

    def _add_soundboard_dashboard_card(self, root, icon: str, title: str, meta: str, build_controls=None) -> QFrame:
        card = QFrame()
        card.setObjectName("appsRouteCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        icon_label = QLabel(icon)
        icon_label.setObjectName("appRouteIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        header.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        title_label = QLabel(title)
        title_label.setObjectName("appRouteName")
        text_col.addWidget(title_label)

        meta_label = QLabel(meta)
        meta_label.setObjectName("appRouteMeta")
        meta_label.setWordWrap(True)
        text_col.addWidget(meta_label)

        header.addLayout(text_col, 1)
        layout.addLayout(header)

        if build_controls is not None:
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(7)
            build_controls(body_layout)
            layout.addWidget(body)

        root.addWidget(card)
        return card

    def _make_soundboard_dashboard_button(self, text: str, tooltip: str, callback, *, minimum_width: int = 118) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("padTopButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(32)
        button.setMinimumWidth(int(minimum_width))
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def _make_soundboard_small_icon_button(self, text: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("soundboardSmallIconButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def _soundboard_route_state_for_dashboard(self) -> dict[str, bool]:
        controller = getattr(self, "backend_controller", None)
        if controller is None:
            return {"send_to_micro": False, "monitor_to_mic_out": False}
        try:
            return dict(controller._soundboard_route_state())
        except Exception:
            return {"send_to_micro": False, "monitor_to_mic_out": False}

    def _soundboard_output_for_dashboard(self) -> str:
        controller = getattr(self, "backend_controller", None)
        if controller is None:
            return "media"
        try:
            key = str(controller.soundboard_output_channel() or "media")
        except Exception:
            key = "media"
        allowed = {key for _label, key in SOUNDBOARD_LOGICAL_OUTPUTS}
        return key if key in allowed else "media"

    def _sync_compact_soundboard_dashboard(self) -> None:
        output_select = getattr(self, "monitoring_output_select", None)
        if output_select is not None:
            current_key = self._soundboard_output_for_dashboard()
            current_label = SOUNDBOARD_LOGICAL_LABEL_BY_KEY.get(current_key, "MEDIA")

            # Synchronize the display without invoking the route callback again.
            if output_select.current_text() != current_label:
                output_select._current = current_label
                output_select._sync_text()

        micro_check = getattr(self, "monitoring_micro_check", None)
        if micro_check is not None:
            route_state = self._soundboard_route_state_for_dashboard()
            wanted = bool(route_state.get("send_to_micro"))

            if micro_check.isChecked() != wanted:
                micro_check.blockSignals(True)
                try:
                    micro_check.setChecked(wanted)
                finally:
                    micro_check.blockSignals(False)

    def _handle_dashboard_backend_status(self, message: str) -> None:
        status = str(message or "")
        if status.startswith("Soundboard output") or status.startswith("Soundboard MICRO"):
            QTimer.singleShot(0, self._sync_compact_soundboard_dashboard)

    def _set_dashboard_soundboard_output(self, label: str) -> None:
        controller = getattr(self, "backend_controller", None)
        if controller is None:
            return
        mapping = {label: key for label, key in SOUNDBOARD_LOGICAL_OUTPUTS}
        key = mapping.get(str(label or "").strip(), "media")
        controller.set_soundboard_output_channel(key)
        QTimer.singleShot(80, self._sync_compact_soundboard_dashboard)

    def _set_dashboard_soundboard_to_micro(self, enabled: bool) -> None:
        controller = getattr(self, "backend_controller", None)
        if controller is not None:
            controller.set_soundboard_micro_enabled(bool(enabled))
            QTimer.singleShot(80, self._sync_compact_soundboard_dashboard)

    def _build_compact_soundboard_dashboard(self, root: QVBoxLayout) -> None:
        route_state = self._soundboard_route_state_for_dashboard()
        current_key = self._soundboard_output_for_dashboard()
        labels = [label for label, _key in SOUNDBOARD_LOGICAL_OUTPUTS]
        current_label = SOUNDBOARD_LOGICAL_LABEL_BY_KEY.get(current_key, "MEDIA")

        monitoring_card = PermanentRouteCard(
            "▦",
            "Monitoring",
            "Choose which channel plays the pads. MICRO also sends pads into your microphone.",
            select_items=labels,
            select_current=current_label,
            select_callback=self._set_dashboard_soundboard_output,
        )

        self.monitoring_card = monitoring_card
        self.monitoring_output_select = getattr(monitoring_card, "select", None)
        if self.monitoring_output_select is not None:
            self.monitoring_output_select.setMaximumWidth(172)

        self.monitoring_micro_check = QCheckBox("MICRO")
        self.monitoring_micro_check.setCursor(Qt.PointingHandCursor)
        self.monitoring_micro_check.setChecked(bool(route_state.get("send_to_micro")))
        self.monitoring_micro_check.toggled.connect(
            self._set_dashboard_soundboard_to_micro
        )

        body = getattr(monitoring_card, "_body", None)
        body_layout = body.layout() if body is not None else None
        if body_layout is not None:
            # Keep Output and its compact selector together and center the pair.
            select_item = body_layout.itemAt(0)
            select_row = select_item.layout() if select_item is not None else None
            if select_row is not None:
                select_row.setSpacing(9)
                select_row.insertStretch(0, 1)
                select_row.addStretch(1)

            open_button = self._make_soundboard_dashboard_button(
                "Open",
                "Open the full Soundboard window.",
                self._detach,
                minimum_width=132,
            )
            _add_balanced_dashboard_action_row(
                body_layout,
                self.monitoring_micro_check,
                open_button,
            )

        root.addWidget(monitoring_card)

        def volume_controls(body: QVBoxLayout) -> None:
            slider_row = QHBoxLayout()
            slider_row.setContentsMargins(0, 0, 0, 0)
            slider_row.setSpacing(8)

            minus = QLabel("-")
            minus.setObjectName("soundboardVolumeSign")
            minus.setAlignment(Qt.AlignCenter)
            slider_row.addWidget(minus)

            self.soundboard_volume_slider = NoWheelSlider(Qt.Horizontal)
            self.soundboard_volume_slider.setObjectName("soundboardVolumeSlider")
            self.soundboard_volume_slider.setRange(0, 100)
            self.soundboard_volume_slider.setValue(int(self._soundboard_volume))
            self.soundboard_volume_slider.setMinimumHeight(24)
            self.soundboard_volume_slider.setTracking(True)

            self.soundboard_volume_value = QLabel(f"{int(self._soundboard_volume)}%")
            self.soundboard_volume_value.setObjectName("appRouteMeta")
            self.soundboard_volume_value.setMinimumWidth(44)
            self.soundboard_volume_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

            def on_volume_changed(value: int) -> None:
                self.soundboard_volume_value.setText(f"{int(value)}%")
                self._queue_soundboard_volume(int(value))

            self.soundboard_volume_slider.valueChanged.connect(on_volume_changed)
            slider_row.addWidget(self.soundboard_volume_slider, 1)

            plus = QLabel("+")
            plus.setObjectName("soundboardVolumeSign")
            plus.setAlignment(Qt.AlignCenter)
            slider_row.addWidget(plus)

            slider_row.addWidget(self.soundboard_volume_value)
            body.addLayout(slider_row)

            options_row = QHBoxLayout()
            options_row.setContentsMargins(0, 0, 0, 0)
            options_row.setSpacing(8)

            self.soundboard_auto_level_check = QCheckBox("Auto level")
            self.soundboard_auto_level_check.setObjectName("soundboardAutoLevelToggle")
            self.soundboard_auto_level_check.setMinimumHeight(26)
            self.soundboard_auto_level_check.setCursor(Qt.PointingHandCursor)
            self.soundboard_auto_level_check.setChecked(bool(self._soundboard_auto_level_enabled))
            self.soundboard_auto_level_check.setToolTip(
                "Auto level enabled: pad sliders become trim controls."
                if self._soundboard_auto_level_enabled
                else "Auto level disabled: pad sliders keep normal volume behavior."
            )
            self.soundboard_auto_level_check.toggled.connect(
                lambda checked: self._set_soundboard_auto_level_enabled(bool(checked))
            )
            options_row.addWidget(self.soundboard_auto_level_check)

            self.soundboard_analyze_button = self._make_soundboard_small_icon_button(
                "↻",
                "Refresh auto-level analysis for all present soundboard files.",
                self._analyze_soundboard_auto_level,
            )
            options_row.addWidget(self.soundboard_analyze_button)

            options_row.addStretch(1)
            body.addLayout(options_row)

        self._add_soundboard_dashboard_card(
            root,
            "🔊",
            "Volume",
            "Global soundboard volume, auto level toggle, and analysis refresh.",
            volume_controls,
        )

        panic_row = QHBoxLayout()
        panic_row.setContentsMargins(0, 2, 0, 0)
        panic_row.setSpacing(8)
        panic_row.addStretch(1)

        self.stop_all_button = QPushButton("■  PANIC  ·  Stop all pads")
        self.stop_all_button.setObjectName("soundboardPanicButton")
        self.stop_all_button.setCursor(Qt.PointingHandCursor)
        self.stop_all_button.setToolTip("Stop all currently playing soundboard pads.")
        self.stop_all_button.clicked.connect(self._stop_all_sounds)
        panic_row.addWidget(self.stop_all_button)

        panic_row.addStretch(1)
        root.addLayout(panic_row)

        root.addStretch(1)


    def __init__(self, detach_callback=None, columns: int = 2, show_detach: bool = True, backend_controller=None):
        super().__init__()
        self.backend_controller = backend_controller
        shared_service = getattr(backend_controller, "soundboard_service", None)
        self.soundboard_service = shared_service or SoundboardService()
        shortcut_state_signal = getattr(backend_controller, "soundboard_shortcut_state_changed", None)
        if shortcut_state_signal is not None:
            shortcut_state_signal.connect(self._sync_soundboard_shortcut_state)
        self._detach_callback = detach_callback
        self._columns = max(1, columns)
        self._show_detach = show_detach
        self._edit_mode = False
        self._bulk_delete_mode = False
        self._active_emoji_card: SoundPadCard | None = None
        self.pad_cards: list[SoundPadCard] = []
        self._soundboard_volume = self._read_soundboard_volume_setting()
        self._soundboard_auto_level_enabled = self._read_soundboard_auto_level_enabled()
        self._active_soundboard_folder: str | None = None
        self._pending_soundboard_volume = self._soundboard_volume
        self._soundboard_volume_timer = QTimer(self)
        self._soundboard_volume_timer.setSingleShot(True)
        self._soundboard_volume_timer.setInterval(120)
        self._soundboard_volume_timer.timeout.connect(self._apply_queued_soundboard_volume)
        self._pad_bg_darkness = self._read_pad_bg_darkness_setting()
        self._pending_pad_bg_darkness = self._pad_bg_darkness
        self._pad_bg_darkness_timer = QTimer(self)
        self._pad_bg_darkness_timer.setSingleShot(True)
        self._pad_bg_darkness_timer.setInterval(120)
        self._pad_bg_darkness_timer.timeout.connect(self._apply_queued_pad_bg_darkness)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        if bool(getattr(self, "_show_detach", True)):
            self._build_compact_soundboard_dashboard(root)

            controller = getattr(self, "backend_controller", None)
            status_signal = getattr(controller, "status_changed", None)
            if status_signal is not None:
                status_signal.connect(self._handle_dashboard_backend_status)

            QTimer.singleShot(0, self._sync_compact_soundboard_dashboard)

            self.pair_overlay = None
            self.pair_code_input = None
            return

        top_bar = QFrame()
        top_bar.setObjectName("padsTopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(7, 7, 7, 7)
        top_layout.setSpacing(6)

        self.stop_all_button = CenterGlyphButton("■", y_offset=-1)
        self.stop_all_button.setObjectName("padStopAllButton")
        self.stop_all_button.setToolTip("Stop all sounds")
        self.stop_all_button.clicked.connect(self._stop_all_sounds)
        top_layout.addWidget(self.stop_all_button)

        # Pair Android lives next to the Soundboard refresh/analyze icon.
        if self._show_detach:
            detach = QPushButton("Detach")
            detach.setObjectName("padTopButton")
            detach.clicked.connect(self._detach)
            top_layout.addWidget(detach)

        self.edit = QPushButton("Edit")
        self.edit.setObjectName("padTopButton")
        self.edit.setCheckable(True)
        self.edit.toggled.connect(self._set_edit_mode)
        top_layout.addWidget(self.edit)

        add = CenterGlyphButton("+", y_offset=-1)
        add.setObjectName("padAddButton")
        add.clicked.connect(self._add_pad)
        top_layout.addWidget(add)

        library_button = QPushButton("Add sounds…")
        library_button.setObjectName("padTopButton")
        library_button.setToolTip("Import multiple sounds, a folder, or an online bank")
        library_button.clicked.connect(self._open_soundboard_library)
        top_layout.addWidget(library_button)

        self.select_sounds_button = QPushButton("Select")
        self.select_sounds_button.setObjectName("padTopButton")
        self.select_sounds_button.setCheckable(True)
        self.select_sounds_button.setToolTip("Select multiple sounds to move or delete")
        self.select_sounds_button.toggled.connect(self._toggle_soundboard_selection)
        top_layout.addWidget(self.select_sounds_button)

        self.bulk_delete = CenterGlyphButton("🗑")
        self.bulk_delete.setObjectName("padBulkDeleteButton")
        self.bulk_delete.setCheckable(True)
        self.bulk_delete.setToolTip("Bulk delete")
        self.bulk_delete.clicked.connect(self._bulk_delete_clicked)
        top_layout.addWidget(self.bulk_delete)

        self.options_button = CenterGlyphButton("▾", y_offset=-1)
        self.options_button.setObjectName("padTopButton")
        self.options_button.setCheckable(True)
        self.options_button.setMinimumWidth(34)
        self.options_button.setMaximumWidth(34)
        self.options_button.setToolTip("Show soundboard visual options")
        self.options_button.toggled.connect(self._set_options_visible)
        top_layout.addWidget(self.options_button)

        top_layout.addStretch(1)
        root.addWidget(top_bar)

        self.selection_actions = QFrame()
        selection_layout = QHBoxLayout(self.selection_actions)
        selection_layout.setContentsMargins(7, 0, 7, 3)
        select_visible = QPushButton("Select visible")
        select_visible.clicked.connect(self._select_visible_sounds)
        selection_layout.addWidget(select_visible)
        move_selected = QPushButton("Move selected to folder…")
        move_selected.clicked.connect(self._move_selected_sounds_to_folder)
        selection_layout.addWidget(move_selected)
        clear_selected = QPushButton("Clear selection")
        clear_selected.clicked.connect(lambda: [card.set_bulk_selected(False) for card in self.pad_cards])
        selection_layout.addWidget(clear_selected)
        selection_layout.addStretch(1)
        self.selection_actions.hide()
        root.addWidget(self.selection_actions)

        self.folder_bar = QFrame()
        self.folder_bar.setObjectName("soundboardFolderBar")
        self.folder_layout = QHBoxLayout(self.folder_bar)
        self.folder_layout.setContentsMargins(7, 5, 7, 5)
        self.folder_layout.setSpacing(6)
        root.addWidget(self.folder_bar)
        self._refresh_folder_bar()

        self.options_panel = QFrame()
        self.options_panel.setObjectName("padBgDarknessControls")
        pad_dark_layout = QHBoxLayout(self.options_panel)
        pad_dark_layout.setContentsMargins(9, 5, 9, 5)
        pad_dark_layout.setSpacing(8)

        pad_dark_label = QLabel("Pad BG darkness")
        pad_dark_label.setObjectName("detachedBgLabel")
        pad_dark_layout.addWidget(pad_dark_label)

        self.pad_bg_darkness_slider = NoWheelSlider(Qt.Horizontal)
        self.pad_bg_darkness_slider.setObjectName("padBgDarknessSlider")
        self.pad_bg_darkness_slider.setRange(0, 100)
        self.pad_bg_darkness_slider.setValue(int(self._pad_bg_darkness))
        self.pad_bg_darkness_slider.setMinimumHeight(24)
        self.pad_bg_darkness_slider.valueChanged.connect(lambda value: self._queue_pad_bg_darkness(int(value)))
        pad_dark_layout.addWidget(self.pad_bg_darkness_slider, 1)

        root.addWidget(self.options_panel)
        self.options_panel.setVisible(False)

        self.grid_scroll = AdaptiveScrollArea()
        self.grid_scroll.setObjectName("padsGridScroll")
        self.grid_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        pads_scrollbar = self.grid_scroll.verticalScrollBar()
        pads_scrollbar.setObjectName("padsGridScrollbar")
        pads_scrollbar.setFixedWidth(8)

        self.grid_host = QWidget()
        self.grid_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(5)

        self.empty_folder_label = QLabel("")
        self.empty_folder_label.setObjectName("muted")
        self.empty_folder_label.setAlignment(Qt.AlignCenter)
        self.empty_folder_label.setWordWrap(True)
        self.empty_folder_label.hide()

        self.grid_scroll.setWidget(self.grid_host)
        self.grid_scroll._schedule_margin_update()
        root.addWidget(self.grid_scroll, 1)

        volume_row = QFrame()
        volume_row.setObjectName("soundboardVolumeControls")
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(7, 4, 7, 4)
        volume_layout.setSpacing(8)

        soundboard_volume_minus = QLabel("-")
        soundboard_volume_minus.setObjectName("soundboardVolumeSign")
        soundboard_volume_minus.setAlignment(Qt.AlignCenter)
        volume_layout.addWidget(soundboard_volume_minus)

        self.soundboard_volume_slider = NoWheelSlider(Qt.Horizontal)
        self.soundboard_volume_slider.setObjectName("soundboardVolumeSlider")
        self.soundboard_volume_slider.setRange(0, 100)
        self.soundboard_volume_slider.setValue(int(self._soundboard_volume))
        self.soundboard_volume_slider.setMinimumHeight(24)
        self.soundboard_volume_slider.valueChanged.connect(lambda value: self._queue_soundboard_volume(int(value)))
        volume_layout.addWidget(self.soundboard_volume_slider, 1)

        soundboard_volume_plus = QLabel("+")
        soundboard_volume_plus.setObjectName("soundboardVolumeSign")
        soundboard_volume_plus.setAlignment(Qt.AlignCenter)
        volume_layout.addWidget(soundboard_volume_plus)

        self.soundboard_auto_level_check = QCheckBox("Auto-level")
        self.soundboard_auto_level_check.setObjectName("soundboardAutoLevelToggle")
        self.soundboard_auto_level_check.setMinimumWidth(96)
        self.soundboard_auto_level_check.setMinimumHeight(24)
        self.soundboard_auto_level_check.setMaximumHeight(24)
        self.soundboard_auto_level_check.setCursor(Qt.PointingHandCursor)
        self.soundboard_auto_level_check.setChecked(bool(self._soundboard_auto_level_enabled))
        self.soundboard_auto_level_check.setText("Auto-level")
        self.soundboard_auto_level_check.setToolTip(
            "Auto-level is enabled. Manual dB trim still applies."
            if self._soundboard_auto_level_enabled
            else "Auto-level is disabled. Only manual dB trim applies."
        )
        self.soundboard_auto_level_check.toggled.connect(
            lambda checked: self._set_soundboard_auto_level_enabled(bool(checked))
        )
        volume_layout.addWidget(self.soundboard_auto_level_check)

        root.addWidget(volume_row)

        for name, icon, meta, slot_key, background_path, trim_db, sound_path, folder_name in self._load_real_soundboard_pads():
            self._add_pad(
                name,
                icon,
                meta,
                slot_key=slot_key,
                background_path=background_path,
                trim_db=trim_db,
                sound_path=sound_path,
                folder_name=folder_name,
            )

        self.emoji_overlay = QFrame(self)
        self.emoji_overlay.setObjectName("emojiPalette")
        palette_layout = QGridLayout(self.emoji_overlay)
        palette_layout.setContentsMargins(8, 8, 8, 8)
        palette_layout.setHorizontalSpacing(4)
        palette_layout.setVerticalSpacing(4)

        for index, emoji in enumerate(self.EMOJIS):
            button = QPushButton(emoji)
            button.setObjectName("emojiChoice")
            button.clicked.connect(lambda _checked=False, value=emoji: self._choose_emoji(value))
            palette_layout.addWidget(button, index // 8, index % 8)

        self.emoji_overlay.hide()

        self.pair_overlay = None
        self.pair_code_input = None

        self._set_pad_bg_darkness(self._pad_bg_darkness, persist=False)
        changed_signal = getattr(self.backend_controller, "soundboard_changed", None)
        if changed_signal is not None:
            changed_signal.connect(self._refresh_from_soundboard_service)
        QTimer.singleShot(0, self._update_responsive_columns)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.MouseButtonPress and hasattr(self, "emoji_overlay") and self.emoji_overlay.isVisible():
            try:
                global_pos = event.globalPosition().toPoint()
            except AttributeError:
                global_pos = event.globalPos()

            top_left = self.emoji_overlay.mapToGlobal(QPoint(0, 0))
            overlay_rect = QRect(top_left, self.emoji_overlay.size())
            if not overlay_rect.contains(global_pos):
                self.emoji_overlay.hide()

        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_responsive_columns()
        self._position_emoji_overlay()
        self._position_pair_overlay()

    def _position_emoji_overlay(self) -> None:
        if not hasattr(self, "emoji_overlay"):
            return
        width = min(300, max(220, self.width() - 28))
        height = min(250, max(170, self.height() - 90))
        x = max(8, (self.width() - width) // 2)
        y = max(48, (self.height() - height) // 2)
        self.emoji_overlay.setGeometry(x, y, width, height)

    def _show_emoji_palette(self, card: SoundPadCard) -> None:
        self._active_emoji_card = card
        self._position_emoji_overlay()
        self.emoji_overlay.show()
        self.emoji_overlay.raise_()

    def _choose_emoji(self, emoji: str) -> None:
        if self._active_emoji_card is not None:
            self._active_emoji_card.set_emoji(emoji)
            self._persist_card_emoji(self._active_emoji_card, emoji)
        self.emoji_overlay.hide()


    def _read_soundboard_slots(self) -> list[dict]:
        try:
            return self.soundboard_service.read_slots()
        except Exception:
            return []

    def _refresh_from_soundboard_service(self) -> None:
        self._set_soundboard_volume(
            self.soundboard_service.read_global_volume(),
            persist=False,
        )
        self._set_soundboard_auto_level_enabled(
            self.soundboard_service.read_auto_level_enabled(),
            persist=False,
        )
        self._reload_soundboard_pads()

    def _icon_for_soundboard_slot(self, slot: dict, path_text: str) -> str:
        explicit = str(slot.get("emoji") or slot.get("icon") or "").strip()
        if explicit:
            return explicit

        output_channel = str(slot.get("output_channel") or "media").strip().lower()
        if output_channel in {"micro", "micro_bus"}:
            return "🎤"
        if output_channel in {"retour", "return-mic", "mic_out"}:
            return "🎙"
        if output_channel == "chat":
            return "💬"
        if output_channel == "game":
            return "🎮"

        suffix = Path(path_text).suffix.lower() if path_text else ""
        if suffix in {".mp3", ".wav", ".ogg", ".flac", ".m4a"}:
            return "🎵"

        return "🎧"

    def _format_soundboard_route(self, value: str) -> str:
        key = str(value or "media").strip().lower()
        names = {
            "all": "ALL",
            "game": "GAME",
            "media": "MEDIA",
            "chat": "CHAT",
            "more": "MORE",
            "retour": "MIC OUT",
            "return-mic": "MIC OUT",
            "mic_out": "MIC OUT",
            "micro": "MICRO",
            "micro_bus": "MICRO",
        }
        return names.get(key, key.upper() if key else "MEDIA")

    def _clean_soundboard_folder_label(self, value) -> str:
        text = str(value or "").strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return "Main"
        if text.casefold() == "all":
            return "All"
        return text[:32]

    def _folder_storage_value(self, label) -> str:
        cleaned = self._clean_soundboard_folder_label(label)
        if cleaned.casefold() in {"main", "all"}:
            return ""
        return cleaned

    def _slot_folder_label(self, slot: dict) -> str:
        return self._clean_soundboard_folder_label(slot.get("folder") if isinstance(slot, dict) else "")

    def _slot_matches_active_folder(self, slot: dict) -> bool:
        active = getattr(self, "_active_soundboard_folder", None)
        if active is None:
            return True
        return self._slot_folder_label(slot).casefold() == str(active).casefold()

    def _available_soundboard_folders(self) -> list[str]:
        data = self._read_soundboard_document()
        seen: set[str] = set()
        folders: list[str] = []

        def add_folder(value) -> None:
            label = self._clean_soundboard_folder_label(value)
            if label.casefold() == "all":
                return
            key = label.casefold()
            if key in seen:
                return
            seen.add(key)
            folders.append(label)

        add_folder("Main")

        for folder in data.get("folders", []):
            add_folder(folder)

        for slot in data.get("slots", []):
            if isinstance(slot, dict):
                add_folder(slot.get("folder"))

        return folders

    def _remember_soundboard_folder(self, data: dict, label: str) -> None:
        storage = self._folder_storage_value(label)
        if not storage:
            return
        folders = data.setdefault("folders", [])
        if not isinstance(folders, list):
            folders = []
            data["folders"] = folders
        if all(str(item).casefold() != storage.casefold() for item in folders):
            folders.append(storage)

    def _clear_layout_widgets(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_folder_bar(self) -> None:
        if not hasattr(self, "folder_layout"):
            return

        self._clear_layout_widgets(self.folder_layout)

        def add_button(label: str, folder_value) -> None:
            button = QPushButton(label)
            button.setObjectName("soundboardFolderPill")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            active = getattr(self, "_active_soundboard_folder", None)
            button.setChecked(
                (folder_value is None and active is None)
                or (
                    folder_value is not None
                    and active is not None
                    and str(folder_value).casefold() == str(active).casefold()
                )
            )
            button.clicked.connect(lambda _checked=False, value=folder_value: self._set_active_soundboard_folder(value))
            self.folder_layout.addWidget(button)

        add_button("All", None)
        for folder in self._available_soundboard_folders():
            add_button(folder, folder)

        add_folder = QPushButton("+")
        add_folder.setObjectName("soundboardFolderAddButton")
        add_folder.setToolTip("Create folder")
        add_folder.setCursor(Qt.PointingHandCursor)
        add_folder.clicked.connect(self._create_soundboard_folder)
        self.folder_layout.addWidget(add_folder)

        self.folder_layout.addStretch(1)

    def _set_active_soundboard_folder(self, folder) -> None:
        self._active_soundboard_folder = None if folder is None else self._clean_soundboard_folder_label(folder)
        self._refresh_folder_bar()
        # Folder switches only reflow the already-created cards. Re-reading
        # metadata, decoding images and rebuilding every widget caused the old
        # visible pause and is unnecessary for navigation.
        self._reflow_grid()

    def _create_soundboard_folder(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Create sound folder",
            "Folder name:",
            QLineEdit.Normal,
            "",
        )
        if not ok:
            return

        label = self._clean_soundboard_folder_label(name)
        if label.casefold() in {"all", "main"}:
            label = "Main"

        data = self._read_soundboard_document()
        self._remember_soundboard_folder(data, label)
        self._write_soundboard_document(data)

        self._active_soundboard_folder = label
        self._refresh_folder_bar()
        self._reflow_grid()

    def _choose_pad_folder(self, card: SoundPadCard) -> None:
        data, slots, index = self._ensure_slot_for_card(card)
        slot = slots[index]

        current = self._slot_folder_label(slot)
        folders = self._available_soundboard_folders()
        if all(folder.casefold() != current.casefold() for folder in folders):
            folders.insert(0, current)

        choices = folders + ["New folder…"]
        try:
            current_index = next(i for i, item in enumerate(choices) if item.casefold() == current.casefold())
        except Exception:
            current_index = 0

        selected, ok = QInputDialog.getItem(
            self,
            "Move sound to folder",
            "Folder:",
            choices,
            current_index,
            False,
        )
        if not ok:
            return

        if selected == "New folder…":
            name, ok = QInputDialog.getText(
                self,
                "Create sound folder",
                "Folder name:",
                QLineEdit.Normal,
                "",
            )
            if not ok:
                return
            selected = self._clean_soundboard_folder_label(name)

        label = self._clean_soundboard_folder_label(selected)
        storage = self._folder_storage_value(label)
        if storage:
            slot["folder"] = storage
            self._remember_soundboard_folder(data, storage)
        else:
            slot["folder"] = ""

        self._write_soundboard_document(data)
        self._refresh_folder_bar()
        card.set_folder_name(label)
        self._reflow_grid()

    def _sync_soundboard_shortcut_state(self, slot_key: str, trigger: str, enabled: bool) -> None:
        wanted = str(slot_key or "").strip()
        if not wanted:
            return
        for card in getattr(self, "pad_cards", []):
            if str(card.slot_key() or "").strip() != wanted:
                continue
            card.sync_shortcut_binding(str(trigger or ""), bool(enabled))

    def _reload_soundboard_pads(self) -> None:
        if not hasattr(self, "grid"):
            return

        self.grid_host.setUpdatesEnabled(False)
        self.grid_scroll.setUpdatesEnabled(False)
        self._loading_soundboard_pads = True
        try:
            for card in list(self.pad_cards):
                self.grid.removeWidget(card)
                card.deleteLater()
            self.pad_cards.clear()

            for name, icon, meta, slot_key, background_path, trim_db, sound_path, folder_name in self._load_real_soundboard_pads():
                self._add_pad(
                    name,
                    icon,
                    meta,
                    slot_key=slot_key,
                    background_path=background_path,
                    trim_db=trim_db,
                    sound_path=sound_path,
                    folder_name=folder_name,
                )
        finally:
            self._loading_soundboard_pads = False
            self.grid_host.setUpdatesEnabled(True)
            self.grid_scroll.setUpdatesEnabled(True)

        self._reflow_grid()
        QTimer.singleShot(0, self._update_responsive_columns)

    def _load_real_soundboard_pads(self) -> list[tuple[str, str, str, str, str, float, str, str]]:
        pads: list[tuple[str, str, str, str, str, float, str, str]] = []

        for index, slot in enumerate(self._read_soundboard_slots(), start=1):
            path_text = str(slot.get("path") or "").strip()
            label = str(slot.get("label") or "").strip()

            if not path_text and not label:
                continue

            if not label or label.upper() in {"SOUND", "EMPTY"}:
                label = Path(path_text).stem if path_text else f"Pad {index}"

            route = self._format_soundboard_route(str(slot.get("output_channel") or "media"))
            icon = self._icon_for_soundboard_slot(slot, path_text)
            slot_key = str(slot.get("id") or "").strip() or str(index)
            background_path = str(slot.get("background_path") or "").strip()
            folder_name = self._slot_folder_label(slot)
            try:
                trim_db = max(-24.0, min(24.0, float(slot.get("trim_db", 0.0) or 0.0)))
            except Exception:
                trim_db = 0.0

            if path_text:
                sound_path = Path(path_text).expanduser()
                filename = sound_path.name or "sound"
                status = route if sound_path.exists() else "missing"
                meta = f"{status} · {filename}"
            else:
                meta = route

            pads.append((label, icon, meta, slot_key, background_path, trim_db, path_text, folder_name))

        if pads:
            return pads

        if SOUNDBOARD_PATH.is_file():
            return [("No saved sounds", "🎧", "soundboard.json empty", "", "", 0.0, "", "Main")]

        return [("No soundboard file", "🎧", "soundboard.json missing", "", "", 0.0, "", "Main")]


    def _stop_all_sounds(self) -> None:
        button = getattr(self, "stop_all_button", None)
        if button is not None:
            button.setEnabled(False)
            QTimer.singleShot(180, lambda b=button: b.setEnabled(True))

        self.soundboard_service.stop_all()

    def _remote_server_reachable(self) -> bool:
        controller = getattr(self, "backend_controller", None)
        config_factory = getattr(controller, "_remote_runtime_config", None)
        config = config_factory() if callable(config_factory) else RemoteRuntimeConfig.from_environment(PROJECT_ROOT)
        return remote_server_reachable(config)

    def _start_android_remote_server_if_needed(self) -> bool:
        controller = getattr(self, "backend_controller", None)
        registry = getattr(controller, "module_registry", None)
        if registry is not None and not registry.remote_runtime_required():
            QMessageBox.warning(self, "Pair Android", "Remote has no enabled Pads or Voice FX surface.")
            return False
        supervisor = getattr(controller, "runtime_supervisor", None)
        try:
            if supervisor is not None:
                ok, message = supervisor.reconcile("remote.server")
            else:
                config_factory = getattr(controller, "_remote_runtime_config", None)
                config = config_factory() if callable(config_factory) else RemoteRuntimeConfig.from_environment(PROJECT_ROOT)
                ok, message = ensure_remote_server(config, python_executable=sys.executable)
        except Exception as exc:
            ok, message = False, str(exc)
        if not ok:
            QMessageBox.warning(
                self,
                "Pair Android",
                f"Could not start Android Remote.\n\n{message}",
            )
        return ok

    def _pair_android_remote(self) -> None:
        controller = getattr(self, "backend_controller", None)
        creator = getattr(controller, "create_remote_pairing_code", None)
        if callable(creator):
            ok, pin, message = creator()
            if not ok:
                QMessageBox.warning(self, "Pair Android", message or "Remote unavailable")
                return
            self._show_pair_overlay(pin)
            return

        if not self._start_android_remote_server_if_needed():
            return

        pin = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = time.time() + 300

        pairing_path = CONFIG_DIR / "soundboard_pairing.json"
        try:
            pairing_path.parent.mkdir(parents=True, exist_ok=True)
            pairing_path.write_text(
                json.dumps(
                    {
                        "pin": pin,
                        "expires_at": expires_at,
                        "created_at": time.time(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                pairing_path.chmod(0o600)
            except Exception:
                pass
        except Exception as exc:
            QMessageBox.warning(self, "Pair Android", f"Could not write pairing code.\n\n{exc}")
            return

        self._show_pair_overlay(pin)


    def _slot_for_key(self, slot_key: str) -> dict | None:
        return self.soundboard_service.slot_for_key(slot_key)

    def _play_slot_to_soundboard_bus(self, slot: dict) -> bool:
        return self.soundboard_service.play_slot(slot)

    def _play_pad(self, slot_key: str) -> None:
        if self._edit_mode or self._bulk_delete_mode:
            return

        try:
            controller = getattr(self.window(), "backend_controller", None)
            # Routes are applied on startup and whenever the Soundboard routing
            # controls change. Do not run heavy pactl route validation on every
            # pad click: it stalls Glass meters/UI exactly when a sound starts.
            slot = self._slot_for_key(slot_key)
            if not slot or not self._play_slot_to_soundboard_bus(slot):
                QMessageBox.warning(self, "Soundboard playback", "This sound could not be found or could not be played.")

            if controller is not None:
                QTimer.singleShot(120, controller._unmute_soundboard_output_streams)
        except Exception as exc:
            QMessageBox.warning(self, "Soundboard playback", f"Could not play this sound.\n\n{exc}")

    def _read_soundboard_document(self) -> dict:
        try:
            return self.soundboard_service.read_document()
        except Exception:
            return {"slots": []}

    def _write_soundboard_document(self, data: dict) -> None:
        self.soundboard_service.write_document(data)

    def _slot_index_for_card(self, slots: list, card: SoundPadCard) -> int | None:
        key = card.slot_key()
        if key:
            for index, slot in enumerate(slots):
                if isinstance(slot, dict) and str(slot.get("id") or "").strip() == key:
                    return index
            if key.isdigit():
                index = int(key) - 1
                if 0 <= index < len(slots):
                    return index
        return None

    def _ensure_slot_for_card(self, card: SoundPadCard) -> tuple[dict, list, int]:
        data = self._read_soundboard_document()
        slots = data.setdefault("slots", [])
        index = self._slot_index_for_card(slots, card)

        if index is None:
            slot = {
                "id": f"glass-{int(time.time() * 1000)}",
                "label": card.display_name() if not card.display_name().startswith("New pad") else "New sound",
                "path": "",
                "background_path": "",
                "folder": self._folder_storage_value(getattr(self, "_active_soundboard_folder", None)),
                "volume": 80,
                "shortcut": "",
                "output_channel": "media",
                "send_to_micro": False,
                "auto_gain": 1.0,
                "analyzed_path": "",
                "trim_db": 0.0,
            }
            self._remember_soundboard_folder(data, slot.get("folder", ""))
            slots.append(slot)
            index = len(slots) - 1
            card.set_slot_key(str(slot["id"]))

        return data, slots, index

    def _refresh_soundboard_after_edit(self) -> None:
        # Glass pads read soundboard.json directly; there is no hidden playback dialog to sync.
        return

    def _persist_card_emoji(self, card: SoundPadCard, emoji: str) -> None:
        data, slots, index = self._ensure_slot_for_card(card)
        slot = slots[index]
        slot["emoji"] = str(emoji or "").strip()
        slot["icon"] = str(emoji or "").strip()
        self._write_soundboard_document(data)
        self._refresh_soundboard_after_edit()

    def _set_card_trim_db(self, card: SoundPadCard, trim_db: float) -> None:
        data, slots, index = self._ensure_slot_for_card(card)
        slot = slots[index]
        try:
            numeric = float(trim_db)
        except Exception:
            numeric = 0.0
        slot["trim_db"] = max(-24.0, min(24.0, numeric))
        self._write_soundboard_document(data)
        self._refresh_soundboard_after_edit()

    def _open_soundboard_library(self) -> None:
        from .ui.soundboard_library_dialog import SoundboardLibraryDialog
        dialog = SoundboardLibraryDialog(
            self, self.soundboard_service, CONFIG_DIR,
            folder=getattr(self, "_active_soundboard_folder", None) or "Main",
            on_import=self._library_import_finished,
        )
        dialog.exec()
        dialog.deleteLater()

    def _library_import_finished(self) -> None:
        self._refresh_folder_bar()
        self._reload_soundboard_pads()

    def _choose_pad_sound(self, card: SoundPadCard) -> None:
        data, slots, index = self._ensure_slot_for_card(card)
        slot = slots[index]
        current = str(slot.get("path") or "").strip()
        start_dir = str(Path(current).expanduser().parent) if current else str(Path.home())

        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Choose pad sound",
            start_dir,
            "Audio files (*.mp3 *.wav *.ogg *.flac *.m4a);;All files (*)",
        )
        if not chosen:
            return

        sound_path = Path(chosen).expanduser()
        slot["path"] = str(sound_path)

        old_label = str(slot.get("label") or "").strip()
        if not old_label or old_label.upper() in {"SOUND", "EMPTY"} or old_label.startswith("New pad") or old_label == "New sound":
            slot["label"] = sound_path.stem
            card.set_display_name(sound_path.stem)

        route = self._format_soundboard_route(str(slot.get("output_channel") or "media"))
        card.set_meta(f"{route} · {sound_path.name}")
        if hasattr(card, "set_missing_sound"):
            card.set_missing_sound(False)
        if hasattr(card, "icon_button"):
            card.icon_button.setText(self._icon_for_soundboard_slot(slot, str(sound_path)))
        self._write_soundboard_document(data)
        self._refresh_soundboard_after_edit()

    def _choose_pad_background(self, card: SoundPadCard) -> None:
        data, slots, index = self._ensure_slot_for_card(card)
        slot = slots[index]
        current = str(slot.get("background_path") or "").strip()
        start_dir = str(Path(current).expanduser().parent) if current else str(Path.home())

        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Choose pad background",
            start_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*)",
        )
        if not chosen:
            return

        bg_path = str(Path(chosen).expanduser())
        slot["background_path"] = bg_path
        card.set_background_path(bg_path)
        self._write_soundboard_document(data)
        self._refresh_soundboard_after_edit()

    def _detach(self) -> None:
        if self._detach_callback is not None:
            self._detach_callback()

    def _set_options_visible(self, enabled: bool) -> None:
        if hasattr(self, "options_panel"):
            self.options_panel.setVisible(bool(enabled))
        if hasattr(self, "options_button"):
            self.options_button.setText("▴" if enabled else "▾")
            self.options_button.setToolTip(
                "Hide soundboard visual options" if enabled else "Show soundboard visual options"
            )

    def _set_edit_mode(self, enabled: bool) -> None:
        self._edit_mode = enabled
        self.edit.setText("Done" if enabled else "Edit")

        if enabled and self._bulk_delete_mode:
            # Edit mode and bulk delete mode are separate.
            self._stop_bulk_delete()

        if not enabled:
            self.emoji_overlay.hide()
            self._stop_bulk_delete()

        self.grid_host.setUpdatesEnabled(False)
        self.grid_scroll.setUpdatesEnabled(False)
        try:
            for card in self.pad_cards:
                card.set_edit_mode(enabled)
        finally:
            self.grid_host.setUpdatesEnabled(True)
            self.grid_scroll.setUpdatesEnabled(True)

        QTimer.singleShot(0, self._update_responsive_columns)

    def _add_pad(self, name: str | None = None, icon: str = "🎧", meta: str = "new", slot_key: str = "", background_path: str = "", trim_db: float = 0.0, sound_path: str = "", folder_name: str = "Main") -> None:
        if name is None or isinstance(name, bool):
            name = f"New pad {len(self.pad_cards) + 1}"
            icon = "+"
            meta = "empty"
            slot_key = ""
            background_path = ""
            trim_db = 0.0
            sound_path = ""
            folder_name = self._clean_soundboard_folder_label(getattr(self, "_active_soundboard_folder", None))

        card = SoundPadCard(
            name,
            icon,
            meta,
            emoji_callback=self._show_emoji_palette,
            delete_callback=self._confirm_delete_card,
            play_callback=self._play_pad,
            slot_key=slot_key,
            background_path=background_path,
            background_callback=self._choose_pad_background,
            sound_callback=self._choose_pad_sound,
            trim_callback=self._set_card_trim_db,
            folder_callback=self._choose_pad_folder,
            shortcut_state_getter=(
                getattr(self.backend_controller, "soundboard_pad_shortcut_state", None)
                if self.backend_controller is not None else None
            ),
            shortcut_callback=(
                getattr(self.backend_controller, "request_soundboard_pad_shortcut_update", None)
                if self.backend_controller is not None else None
            ),
            trim_db=trim_db,
            folder_name=folder_name,
        )
        if hasattr(card, "set_always_show_meta"):
            card.set_always_show_meta(not bool(self._show_detach))
        if hasattr(card, "set_pad_background_darkness"):
            card.set_pad_background_darkness(self._pad_bg_darkness)
        if background_path:
            card.set_background_path(background_path)
        if str(meta or "").startswith("missing ·") and hasattr(card, "set_missing_sound"):
            card.set_missing_sound(True, sound_path)
        card.set_edit_mode(self._edit_mode)
        card.set_bulk_select_enabled(self._bulk_delete_mode)
        self.pad_cards.append(card)
        if not getattr(self, "_loading_soundboard_pads", False):
            self._reflow_grid()

    def _reflow_grid(self) -> None:
        active = getattr(self, "_active_soundboard_folder", None)
        visible_cards = []
        for card in self.pad_cards:
            self.grid.removeWidget(card)
            visible = active is None or card.folder_name().casefold() == str(active).casefold()
            card.setVisible(visible)
            if visible:
                visible_cards.append(card)

        empty_label = getattr(self, "empty_folder_label", None)
        if empty_label is not None:
            self.grid.removeWidget(empty_label)
            empty_label.hide()

        for index, card in enumerate(visible_cards):
            row = index // self._columns
            col = index % self._columns
            self.grid.addWidget(card, row, col)

        if not visible_cards and empty_label is not None:
            label = str(active or "this view")
            empty_label.setText(f"No sounds in {label}")
            empty_label.show()
            self.grid.addWidget(empty_label, 0, 0, 1, max(1, self._columns))

        max_columns_to_clear = max(len(visible_cards), self._columns + 8, 16)
        for col in range(max_columns_to_clear):
            self.grid.setColumnMinimumWidth(col, 0)
            self.grid.setColumnStretch(col, 0)

        for col in range(self._columns):
            self.grid.setColumnMinimumWidth(col, 0)
            self.grid.setColumnStretch(col, 1)

        self._apply_responsive_card_heights()

    def _remove_card(self, card: SoundPadCard) -> None:
        if card not in self.pad_cards:
            return
        self.grid.removeWidget(card)
        self.pad_cards.remove(card)
        if self._active_emoji_card is card:
            self._active_emoji_card = None
            self.emoji_overlay.hide()
        card.setParent(None)
        card.deleteLater()
        self._reflow_grid()

    def _confirm_delete_card(self, card: SoundPadCard) -> None:
        reply = QMessageBox.question(
            self,
            "Delete sound",
            f'Delete "{card.display_name()}"?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._remove_card(card)

    def _toggle_soundboard_selection(self, enabled: bool) -> None:
        if enabled:
            if self._edit_mode:
                self._set_edit_mode(False)
            self._start_bulk_delete()
        else:
            self._stop_bulk_delete()

    def _select_visible_sounds(self) -> None:
        for card in self.pad_cards:
            if not card.isHidden():
                card.set_bulk_selected(True)

    def _move_selected_sounds_to_folder(self) -> None:
        selected = [card for card in self.pad_cards if card.is_bulk_selected()]
        if not selected:
            QMessageBox.information(self, "Move sounds", "Select the sounds you want to move first.")
            return
        name, ok = QInputDialog.getItem(
            self, "Move selected sounds", "Choose a folder or type a new name:",
            self._available_soundboard_folders(), 0, True,
        )
        if not ok or not str(name).strip():
            return
        # Resolve against the latest document only after the chooser closes.
        data = self._read_soundboard_document()
        slots = data.get("slots", [])
        indices = [self._slot_index_for_card(slots, card) for card in selected]
        from .soundboard_library import move_slots_to_folder
        updated, moved = move_slots_to_folder(data, [i for i in indices if i is not None], name)
        if moved:
            try:
                self._write_soundboard_document(updated)
            except Exception as exc:
                QMessageBox.warning(self, "Move sounds", f"Could not save the folder change.\n\n{exc}")
                return
        self._stop_bulk_delete()
        self._active_soundboard_folder = self._clean_soundboard_folder_label(name)
        if self._active_soundboard_folder.casefold() == "all":
            self._active_soundboard_folder = "Main"
        self._refresh_folder_bar()
        self._reload_soundboard_pads()

    def _bulk_delete_clicked(self) -> None:
        if not self._bulk_delete_mode:
            self._start_bulk_delete()
            return

        self.bulk_delete.setChecked(True)
        selected = [card for card in self.pad_cards if card.is_bulk_selected()]
        count = len(selected)

        if count <= 0:
            QMessageBox.information(self, "Bulk delete", "No sounds selected.")
            return

        reply = QMessageBox.question(
            self,
            "Bulk delete",
            f"Delete {count} selected sound(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for card in list(selected):
            self._remove_card(card)

        self._stop_bulk_delete()

    def _start_bulk_delete(self) -> None:
        # Bulk delete is a selection mode, but the top bar still needs a clear "Done"
        # escape button. Do not enable per-pad edit actions here.
        if self._edit_mode:
            self._set_edit_mode(False)
        self._bulk_delete_mode = True
        self.bulk_delete.setChecked(True)
        if hasattr(self, "select_sounds_button"):
            self.select_sounds_button.blockSignals(True)
            self.select_sounds_button.setChecked(True)
            self.select_sounds_button.blockSignals(False)
            self.select_sounds_button.setText("Selecting")
            self.selection_actions.show()

        self.edit.blockSignals(True)
        self.edit.setChecked(True)
        self.edit.blockSignals(False)
        self.edit.setText("Done")

        self.emoji_overlay.hide()

        for card in self.pad_cards:
            card.set_bulk_select_enabled(True)

    def _stop_bulk_delete(self) -> None:
        self._bulk_delete_mode = False
        if hasattr(self, "select_sounds_button"):
            self.select_sounds_button.blockSignals(True)
            self.select_sounds_button.setChecked(False)
            self.select_sounds_button.blockSignals(False)
            self.select_sounds_button.setText("Select")
            self.selection_actions.hide()
        if hasattr(self, "bulk_delete"):
            self.bulk_delete.setChecked(False)

        for card in getattr(self, "pad_cards", []):
            card.set_bulk_select_enabled(False)

        if hasattr(self, "edit"):
            self.edit.blockSignals(True)
            self.edit.setChecked(bool(self._edit_mode))
            self.edit.blockSignals(False)
            self.edit.setText("Done" if self._edit_mode else "Edit")


class DetachedPadsWindow(QMainWindow):
    def __init__(self, backend_controller=None):
        super().__init__()
        # Closing the detached Soundboard must really destroy it. Keeping a
        # hidden detached window alive makes it continue receiving expensive
        # visual sync updates after the user has closed it.
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._backend_controller = backend_controller
        self._background_settings_signature: tuple[bool, str] | None = None
        self._geometry_settings_key = "glass_soundboard_geometry"
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(550)
        self._geometry_save_timer.timeout.connect(self._save_geometry_now)
        self._background_refresh_timer = QTimer(self)
        self._background_refresh_timer.setSingleShot(True)
        self._background_refresh_timer.setInterval(120)
        self._background_refresh_timer.timeout.connect(self._refresh_background_now)

        self.setWindowTitle("K-Sounds Soundboard")
        self.setMinimumSize(420, 320)
        _restore_window_geometry(self, self._geometry_settings_key, 720, 520, 420, 320)

        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        self._background_source = QPixmap()
        self._background_scaled_cache = QPixmap()
        self._background_saturation = 1.0
        self._background_contrast = 1.0
        self._background_darkness = 130
        self._glass_opacity = 178
        self._load_visual_settings_from_backend()

        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)

        stack = QStackedLayout(central)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setStackingMode(QStackedLayout.StackAll)

        self.background_label = StaticBlurLabel()
        self.background_label.setObjectName("backgroundImage")
        self.background_label.setScaledContents(True)
        self.background_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.background_blur = QGraphicsBlurEffect(self.background_label)
        self.background_blur.setBlurRadius(18.0)
        self.background_label.setGraphicsEffect(self.background_blur)

        self.background_wash = QFrame()
        self.background_wash.setObjectName("backgroundWash")
        self.background_wash.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.background_tint = QFrame()
        self.background_tint.setObjectName("backgroundTint")
        self.background_tint.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        foreground = QWidget()
        foreground.setObjectName("foreground")

        stack.addWidget(self.background_label)
        stack.addWidget(self.background_wash)
        stack.addWidget(self.background_tint)
        stack.addWidget(foreground)
        stack.setCurrentWidget(foreground)

        self.background_label.lower()
        self.background_wash.raise_()
        self.background_tint.raise_()
        foreground.raise_()

        layout = QVBoxLayout(foreground)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        panel = PadsPanel(
            detach_callback=None,
            columns=4,
            show_detach=False,
            backend_controller=self._backend_controller,
        )
        layout.addWidget(panel, 1)

        self.sync_visual_settings()

    @staticmethod
    def _visual_setting_value(settings, attribute: str, fallback: int) -> int:
        try:
            return max(0, min(100, int(getattr(settings, attribute, fallback))))
        except Exception:
            return max(0, min(100, int(fallback)))

    def _load_visual_settings_from_backend(self) -> None:
        settings = getattr(self._backend_controller, "settings", None)
        if settings is None:
            self._background_source = QPixmap(str(APP_BG)) if APP_BG.is_file() else QPixmap()
            return

        enabled = bool(getattr(settings, "wallpaper_enabled", False))
        path = str(getattr(settings, "wallpaper_path", "") or "")
        self._background_settings_signature = (enabled, path)
        self._background_source = _background_source_for_settings(settings)
        self._background_scaled_cache = QPixmap()
        self._background_saturation = _background_factor_from_slider(
            self._visual_setting_value(settings, "glass_background_saturation", 50)
        )
        self._background_contrast = _background_factor_from_slider(
            self._visual_setting_value(settings, "glass_background_contrast", 50)
        )
        self._background_darkness = int(
            40 + self._visual_setting_value(settings, "glass_background_darkness", 55) * 2.0
        )
        self._glass_opacity = int(
            70 + self._visual_setting_value(settings, "glass_opacity", 70) * 1.55
        )

    def sync_visual_settings(self, settings=None, changed_key: str = "") -> None:
        if settings is None:
            settings = getattr(self._backend_controller, "settings", None)
        if settings is None:
            return

        key = str(changed_key or "").strip().lower()
        enabled = bool(getattr(settings, "wallpaper_enabled", False))
        path = str(getattr(settings, "wallpaper_path", "") or "")
        signature = (enabled, path)
        source_changed = signature != self._background_settings_signature
        if source_changed:
            self._background_settings_signature = signature
            self._background_source = _background_source_for_settings(settings)
            self._background_scaled_cache = QPixmap()

        values = {
            "blur": self._visual_setting_value(settings, "glass_background_blur", 18),
            "saturation": self._visual_setting_value(settings, "glass_background_saturation", 50),
            "contrast": self._visual_setting_value(settings, "glass_background_contrast", 50),
            "darkness": self._visual_setting_value(settings, "glass_background_darkness", 55),
            "glass": self._visual_setting_value(settings, "glass_opacity", 70),
        }

        self._background_saturation = _background_factor_from_slider(values["saturation"])
        self._background_contrast = _background_factor_from_slider(values["contrast"])
        self._background_darkness = int(40 + values["darkness"] * 2.0)
        self._glass_opacity = int(70 + values["glass"] * 1.55)

        # Avoid rebuilding unrelated detached effects on saturation/contrast.
        if hasattr(self, "background_blur") and key in {"", "blur"}:
            self.background_blur.setBlurRadius(float(values["blur"]))

        if key in {"", "darkness", "glass"}:
            self._apply_detached_visual_style()

        if source_changed or key in {"", "background_enabled", "background_path", "saturation", "contrast"}:
            self._refresh_background()

    def _apply_detached_visual_style(self) -> None:
        glass = max(0, min(255, self._glass_opacity))
        hover = max(0, min(255, glass + 18))
        dim = max(0, min(255, self._background_darkness))
        wash = 0

        self.setStyleSheet(f"""
QFrame#backgroundWash {{
    background: rgba(90, 130, 255, {wash});
    border: none;
}}

QFrame#backgroundTint {{
    background: rgba(0, 0, 0, {dim});
    border: none;
}}

QFrame#padsTopBar {{
    background: rgba(0, 0, 0, {max(95, glass - 85)});
    border: none;
    border-radius: 12px;
}}

QFrame#soundPadCard {{
    background: rgba(0, 0, 0, {glass});
    border: none;
    border-radius: 12px;
}}

QFrame#soundPadCard:hover {{
    background: rgba(8, 24, 38, {hover});
    border: none;
}}

QFrame#soundPadCard[edit="true"] {{
    background: rgba(11, 31, 47, {max(188, glass)});
    border: none;
}}

QFrame#soundPadCard[bulkSelected="true"] {{
    background: rgba(150, 32, 46, 215);
    border: none;
}}
""")

    def _save_geometry_now(self) -> None:
        _save_window_geometry(self, self._geometry_settings_key)

    def _restore_geometry_after_show(self) -> None:
        _restore_window_geometry(self, self._geometry_settings_key, 720, 520, 420, 320)

    def _queue_geometry_save(self) -> None:
        _queue_window_geometry_save(self, self._geometry_settings_key, self._geometry_save_timer)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._restore_geometry_after_show)
        QTimer.singleShot(350, self._restore_geometry_after_show)

    def closeEvent(self, event) -> None:
        self._save_geometry_now()
        super().closeEvent(event)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._queue_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # QLabel scaledContents handles live resize; do not reprocess the image.
        self._queue_geometry_save()

    def _refresh_background(self) -> None:
        self._background_refresh_timer.start()

    def _refresh_background_now(self) -> None:
        if self._background_source.isNull():
            self._background_scaled_cache = QPixmap()
            self.background_label.clear()
            return

        scaled = self._background_scaled_cache
        if scaled.isNull():
            scaled = self._background_source.scaled(
                QSize(1280, 720),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self._background_scaled_cache = scaled

        self.background_label.setPixmap(
            _adjust_background_pixmap(
                scaled,
                self._background_saturation,
                self._background_contrast,
            )
        )

class DetachedVoiceFxWindow(QMainWindow):
    """Small detached Voice FX surface sharing the central native VFX state."""

    def __init__(self, backend_controller=None):
        super().__init__()
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._backend_controller = backend_controller
        self.setWindowTitle("K-Sounds Voice FX")
        self.setMinimumSize(420, 330)
        self.resize(520, 410)
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        host = QFrame()
        host.setObjectName("drawer")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        state = backend_controller.micro_voice_fx_state() if backend_controller is not None else {}
        self.surface = VoiceFxDialog(
            self,
            state=state,
            apply_callback=backend_controller.set_micro_voice_fx_state if backend_controller is not None else (lambda _updates: False),
            embedded=True,
            custom_presets_provider=backend_controller.voice_fx_user_presets if backend_controller is not None else None,
            custom_state=backend_controller.micro_custom_fx_state() if backend_controller is not None else {},
            custom_apply_callback=backend_controller.set_micro_custom_fx_state if backend_controller is not None else None,
        )
        layout.addWidget(self.surface, 1)
        self.setCentralWidget(host)

        if backend_controller is not None:
            backend_controller.micro_voice_fx_state_changed.connect(self.sync_state)

    def sync_state(self, *_args) -> None:
        if self._backend_controller is None:
            return
        self.surface.sync_state(self._backend_controller.micro_voice_fx_state())
        self.surface.sync_custom_state(self._backend_controller.micro_custom_fx_state())


class Drawer(AndroidPairOverlayMixin, QFrame):
    def __init__(self, visual_callback=None, backend_controller=None, micro_eq_callback=None, module_callback=None):
        super().__init__()
        self.setObjectName("drawer")
        self.setFixedWidth(358)
        self._visual_callback = visual_callback
        self._backend_controller = backend_controller
        self._micro_eq_callback = micro_eq_callback
        self._module_callback = module_callback
        self._module_registry = getattr(backend_controller, "module_registry", None)
        self._module_manager = getattr(backend_controller, "module_manager", None)
        self._visual_sliders: dict[str, QSlider] = {}
        self._shortcut_runtime_state = None
        self._page_indices: dict[str, int] = {}
        self._external_page_specs: dict[str, ExtensionSurface] = {}
        self._external_settings_specs: tuple[ExtensionSurface, ...] = ()
        self._external_action_status_labels: dict[str, QLabel] = {}
        self._external_control_widgets: dict[tuple[str, str, str], object] = {}

        if backend_controller is not None:
            backend_controller.module_ui_action_result.connect(
                self._on_external_action_result
            )

        self.tune_page = None
        self.voice_fx_page = None
        self.pads_panel = None

        self.stack = QStackedWidget()
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(self.stack)

        self._add_page("apps", self._apps_page())
        if self._module_is_enabled("eq"):
            self._add_page("eq", self._eq_page())
        if self._module_is_enabled("micro_tune"):
            self._add_page("micro_tune", self._tune_page())
        if self._module_is_enabled("voice_fx"):
            self._add_page("voice_fx", self._voice_fx_page())
        if self._module_is_enabled("pads"):
            self._add_page("pads", self._pads_page())
        self._add_page("settings", self._settings_page())
        self.refresh_external_pages()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_pair_overlay()

    def _module_is_enabled(self, module_id: str) -> bool:
        registry = self._module_registry
        return True if registry is None else bool(registry.is_enabled(module_id))

    def _add_page(self, route: str, widget: QWidget) -> None:
        setattr(widget, "_ksounds_route", str(route))
        self._page_indices[str(route)] = self.stack.addWidget(widget)

    def _make_scroll_page(self, content: QWidget) -> QScrollArea:
        scroll = AdaptiveScrollArea()
        scroll.setWidget(content)
        scroll._update_scroll_margin()
        return scroll

    def _notify_visual(self, key: str, value) -> None:
        if self._visual_callback is not None:
            self._visual_callback(key, value)

    def sync_visual_settings(self, settings, changed_key: str = "") -> None:
        values = {
            "blur": int(getattr(settings, "glass_background_blur", 18)),
            "saturation": int(getattr(settings, "glass_background_saturation", 50)),
            "contrast": int(getattr(settings, "glass_background_contrast", 50)),
            "darkness": int(getattr(settings, "glass_background_darkness", 55)),
            "glass": int(getattr(settings, "glass_opacity", 70)),
        }
        for key, slider in self._visual_sliders.items():
            slider.blockSignals(True)
            slider.setValue(max(0, min(100, values[key])))
            slider.blockSignals(False)

        if hasattr(self, "micro_processing_ab_shortcut_enabled_check"):
            shortcut_enabled = bool(
                getattr(settings, "micro_processing_ab_shortcut_enabled", True)
            )
            self.micro_processing_ab_shortcut_enabled_check.blockSignals(True)
            self.micro_processing_ab_shortcut_enabled_check.setChecked(shortcut_enabled)
            self.micro_processing_ab_shortcut_enabled_check.blockSignals(False)

        if hasattr(self, "micro_processing_ab_shortcut_edit"):
            self.micro_processing_ab_shortcut_edit.blockSignals(True)
            self.micro_processing_ab_shortcut_edit.setShortcutText(
                str(getattr(settings, "micro_processing_ab_shortcut", "Ctrl+B") or "")
            )
            self.micro_processing_ab_shortcut_edit.blockSignals(False)

        # Runtime KDE authority wins over the persisted fallback values above.
        runtime = getattr(self, "_shortcut_runtime_state", None)
        if runtime is not None:
            self.sync_shortcut_runtime(
                runtime,
                bool(getattr(settings, "micro_processing_ab_shortcut_enabled", True)),
            )

        if hasattr(self, "background_enabled_check"):
            self.background_enabled_check.blockSignals(True)
            self.background_enabled_check.setChecked(bool(getattr(settings, "wallpaper_enabled", False)))
            self.background_enabled_check.blockSignals(False)

        if hasattr(self, "background_path_label"):
            path = str(getattr(settings, "wallpaper_path", "") or "")
            self.background_path_label.setText(Path(path).name if path else "Default background")

        window = getattr(self, "_detached_pads_window", None)
        if window is not None and window.isVisible():
            sync = getattr(window, "sync_visual_settings", None)
            if callable(sync):
                sync(settings, changed_key)

    def sync_shortcut_runtime(self, state, app_enabled: bool) -> None:
        """Reflect KDE's authoritative shortcut state in Settings.

        KDE owns registration and the assigned key. K-Sounds only owns the
        secondary app-level enable switch. When the portal is unavailable we
        keep the old in-app fallback editor usable.
        """

        self._shortcut_runtime_state = state
        if not hasattr(self, "micro_processing_ab_shortcut_enabled_check"):
            return

        authoritative = bool(getattr(state, "portal_ready", False))
        registered = bool(getattr(state, "registered", False))
        kde_enabled = bool(getattr(state, "kde_enabled", False))
        kde_trigger = str(getattr(state, "kde_trigger", "") or "").strip()
        backend = str(getattr(state, "backend", "") or "").strip()
        inline_remap = bool(getattr(state, "inline_remap", False))

        check = self.micro_processing_ab_shortcut_enabled_check
        edit = self.micro_processing_ab_shortcut_edit
        status = getattr(self, "micro_processing_ab_kde_status", None)
        configure = getattr(self, "micro_processing_ab_configure_kde_button", None)

        check.blockSignals(True)
        if authoritative:
            check.setChecked(bool(app_enabled) and kde_enabled)
            check.setEnabled(kde_enabled)
        else:
            check.setChecked(bool(app_enabled))
            check.setEnabled(True)
        check.blockSignals(False)

        edit.blockSignals(True)
        if authoritative:
            edit.setEnabled(inline_remap)
            if kde_enabled and kde_trigger:
                try:
                    sequence = QKeySequence(kde_trigger, QKeySequence.NativeText)
                except Exception:
                    sequence = QKeySequence(kde_trigger)
                if not sequence.isEmpty():
                    edit.setShortcutText(sequence.toString(QKeySequence.PortableText))
            else:
                edit.setShortcutText("")
        else:
            # Local fallback remains editable regardless of the secondary app
            # enable switch, matching the normal hotkey-editor UX.
            edit.setEnabled(True)
        edit.blockSignals(False)

        if status is not None:
            detail = str(getattr(state, "detail", "") or "").strip()
            if authoritative and backend == "kglobalaccel" and "already used" in detail.lower():
                status.setText(f"Conflict: {detail}")
            elif authoritative and backend == "kglobalaccel" and kde_enabled:
                status.setText(f"Global: {kde_trigger or 'active'}")
            elif authoritative and backend == "kglobalaccel" and registered:
                status.setText("Global shortcut cleared — choose a key above")
            elif authoritative and kde_enabled:
                status.setText(f"Global: {kde_trigger or 'active'} — desktop managed")
            elif authoritative and registered:
                status.setText("Desktop global shortcut disabled")
            elif authoritative:
                status.setText("Desktop global shortcut not registered")
            else:
                status.setText(
                    "Global backend unavailable — in-app fallback"
                    + (f" ({detail})" if detail else "")
                )


    def _apps_page(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        title = QLabel("Apps")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        root.addWidget(AppsPanel(self._backend_controller))

        return self._make_scroll_page(content)


    def _eq_page(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        title = QLabel("EQ editor")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        self.eq_panel = EqPanel(self._backend_controller)
        root.addWidget(self.eq_panel, 1)

        return self._make_scroll_page(content)


    def _tune_page(self) -> QWidget:
        state = self._backend_controller.micro_processing_state() if self._backend_controller is not None else {}
        self.tune_page = MicroProcessingDialog(
            self,
            state=state,
            apply_callback=self._backend_controller.set_micro_processing_state,
            noise_suppression_available=bool(state.get("noise_suppression_available", False)),
            open_eq_callback=self._micro_eq_callback,
            activity_callback=self._backend_controller.micro_processing_activity,
            embedded=True,
        )
        return self.tune_page

    def _voice_fx_page(self) -> QWidget:
        content = QWidget()
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        state = self._backend_controller.micro_voice_fx_state() if self._backend_controller is not None else {}

        # Match the Soundboard page rhythm: compact module header, then the
        # shared Monitoring card, then the editor.
        header = QFrame()
        header.setObjectName("sectionHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title = QLabel("Voice FX")
        title.setObjectName("sectionTitle")
        header_layout.addWidget(title)

        self.voice_fx_status_badge = QLabel("Off")
        self.voice_fx_status_badge.setObjectName("remoteStatusBadge")
        self.voice_fx_status_badge.setToolTip("Voice FX active on the physical MICRO input")
        header_layout.addWidget(self.voice_fx_status_badge)
        header_layout.addStretch(1)
        pair_button = QPushButton("📡  Remote")
        pair_button.setObjectName("padTopButton")
        pair_button.setCursor(Qt.PointingHandCursor)
        pair_button.setToolTip("Pair the shared Android Remote")
        pair_button.clicked.connect(self._pair_remote_from_voice_fx)
        pair_button.setVisible(self._module_is_enabled("remote"))
        self.voice_fx_pair_button = pair_button
        header_layout.addWidget(pair_button)
        root.addWidget(header)

        monitoring_card = PermanentRouteCard(
            "✦",
            "Monitoring",
            "Voice FX applied to the physical MICRO input.",
        )
        self.voice_fx_monitoring_card = monitoring_card
        monitoring_card.setMinimumWidth(0)
        monitoring_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        body = getattr(monitoring_card, "_body", None)
        body_layout = body.layout() if body is not None else None
        if body_layout is not None:
            active_row = QHBoxLayout()
            active_row.setContentsMargins(0, 0, 0, 0)
            active_row.setSpacing(9)
            active_row.insertStretch(0, 1)

            active_caption = QLabel("Active")
            active_caption.setObjectName("appRouteMeta")
            active_row.addWidget(active_caption)

            self.voice_fx_monitor_active = QLabel("Off")
            self.voice_fx_monitor_active.setObjectName("voiceFxStatusPill")
            self.voice_fx_monitor_active.setAlignment(Qt.AlignCenter)
            self.voice_fx_monitor_active.setMinimumWidth(0)
            self.voice_fx_monitor_active.setMaximumWidth(172)
            self.voice_fx_monitor_active.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            active_row.addWidget(self.voice_fx_monitor_active, 1)
            active_row.addStretch(1)
            body_layout.addLayout(active_row)

            self.voice_fx_monitor_strength = QLabel("Strength 0%")
            self.voice_fx_monitor_strength.setObjectName("appRouteMeta")
            self.voice_fx_monitor_strength.setAlignment(Qt.AlignCenter)

            actions_host = QWidget()
            actions_layout = QHBoxLayout(actions_host)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(6)

            open_button = QPushButton("Open")
            open_button.setObjectName("padTopButton")
            open_button.setCursor(Qt.PointingHandCursor)
            open_button.setToolTip("Open Voice FX in a detached window.")
            open_button.setMinimumHeight(32)
            open_button.clicked.connect(self._detach_voice_fx)
            actions_layout.addWidget(open_button, 1)

            lab_button = QPushButton("Lab")
            lab_button.setObjectName("padTopButton")
            lab_button.setCursor(Qt.PointingHandCursor)
            lab_button.setToolTip("Open the temporary modular DSP Preset Lab.")
            lab_button.setMinimumHeight(32)
            lab_button.clicked.connect(self._open_preset_lab)
            actions_layout.addWidget(lab_button, 1)

            _add_balanced_dashboard_action_row(
                body_layout,
                self.voice_fx_monitor_strength,
                actions_host,
            )

        root.addWidget(monitoring_card)

        editor_shell = QFrame()
        editor_shell.setObjectName("voiceFxEditorShell")
        editor_shell.setMinimumWidth(0)
        editor_shell.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        editor_shell.setStyleSheet(
            "QFrame#voiceFxEditorShell {"
            "background: rgba(8, 12, 18, 150);"
            "border: 1px solid rgba(104, 184, 225, 34);"
            "border-radius: 12px;"
            "}"
        )
        editor_layout = QVBoxLayout(editor_shell)
        editor_layout.setContentsMargins(8, 8, 8, 8)
        editor_layout.setSpacing(0)

        self.voice_fx_page = VoiceFxDialog(
            editor_shell,
            state=state,
            apply_callback=self._backend_controller.set_micro_voice_fx_state,
            embedded=True,
            custom_presets_provider=self._backend_controller.voice_fx_user_presets,
            custom_state=self._backend_controller.micro_custom_fx_state(),
            custom_apply_callback=self._backend_controller.set_micro_custom_fx_state,
        )
        editor_layout.addWidget(self.voice_fx_page)
        root.addWidget(editor_shell)
        root.addStretch(1)
        self._sync_voice_fx_monitoring(state)
        scroll = self._make_scroll_page(content)
        scroll.set_content_width_locked(True)
        return scroll

    def _sync_voice_fx_monitoring(self, state: dict) -> None:
        if not isinstance(state, dict):
            state = {}
        labels = {
            "distortion": "Distortion",
            "pitch_down": "Pitch ↓",
            "pitch_up": "Pitch ↑",
            "radio": "Radio Pilot",
            "telephone": "Telephone",
            "robot": "Robot 1",
            "reverb": "Reverb",
            "church": "Church",
            "church_pa": "Church PA",
        }
        active = [str(item or "").strip().lower() for item in state.get("active", []) if str(item or "").strip()]
        active_names = [labels.get(key, key) for key in active]
        custom_state = self._backend_controller.micro_custom_fx_state() if self._backend_controller is not None else {}
        custom_preset = custom_state.get("preset", {}) if isinstance(custom_state, dict) else {}
        custom_enabled = bool(custom_state.get("enabled", False)) if isinstance(custom_state, dict) else False
        if custom_enabled and isinstance(custom_preset, dict) and custom_preset.get("modules"):
            active_names.append(str(custom_preset.get("name", "Custom preset") or "Custom preset"))
        full_active_text = " + ".join(active_names) if active_names else "Off"
        compact_active_text = (
            active_names[0]
            if len(active_names) == 1
            else (f"{len(active_names)} effects" if active_names else "Off")
        )

        active_label = getattr(self, "voice_fx_monitor_active", None)
        if active_label is not None:
            active_label.setText(compact_active_text)
            active_label.setToolTip(full_active_text)
            active_label.setStyleSheet(
                "color: rgba(120, 225, 255, 245); font-weight: 800;"
                if active_names
                else "color: rgba(190, 205, 218, 190); font-weight: 750;"
            )

        badge = getattr(self, "voice_fx_status_badge", None)
        if badge is not None:
            if active_names:
                badge.setText(f"{len(active_names)} active")
                badge.setToolTip(full_active_text)
                badge.setStyleSheet(
                    "color: rgba(205, 250, 235, 250);"
                    "background: rgba(18, 82, 68, 130);"
                    "border: 1px solid rgba(80, 210, 170, 125);"
                    "border-radius: 8px; padding: 3px 8px;"
                    "font-size: 10px; font-weight: 700;"
                )
            else:
                badge.setText("Off")
                badge.setToolTip("No Voice FX active")
                badge.setStyleSheet(
                    "color: rgba(190, 220, 238, 225);"
                    "background: rgba(18, 42, 57, 125);"
                    "border: 1px solid rgba(90, 160, 195, 105);"
                    "border-radius: 8px; padding: 3px 8px;"
                    "font-size: 10px; font-weight: 700;"
                )

        strength = getattr(self, "voice_fx_monitor_strength", None)
        if strength is not None:
            try:
                amount = max(0, min(100, int(state.get("amount", 72))))
            except Exception:
                amount = 72
            if custom_enabled and isinstance(custom_preset, dict):
                try:
                    custom_wet = max(0, min(100, int(round(float(custom_preset.get("wet_percent", 100.0))))))
                except Exception:
                    custom_wet = 100
                strength.setText(
                    f"Strength {amount}% · Custom wet {custom_wet}%"
                    if active
                    else f"Custom wet {custom_wet}%"
                )
            else:
                strength.setText(f"Strength {amount}%")

    def _pair_remote_from_voice_fx(self) -> None:
        controller = self._backend_controller
        creator = getattr(controller, "create_remote_pairing_code", None)
        if not callable(creator):
            return
        ok, pin, message = creator()
        if not ok:
            QMessageBox.warning(self, "Pair Android", message or "Remote unavailable")
            return
        self._show_pair_overlay(pin)

    def _detach_voice_fx(self) -> None:
        window = getattr(self, "_detached_voice_fx_window", None)
        if window is not None and window.isVisible():
            window.raise_()
            window.activateWindow()
            return
        window = DetachedVoiceFxWindow(self._backend_controller)
        self._detached_voice_fx_window = window
        window.destroyed.connect(self._clear_detached_voice_fx_window)
        window.show()

    def _clear_detached_voice_fx_window(self, *_args) -> None:
        self._detached_voice_fx_window = None

    def _open_preset_lab(self) -> None:
        window = getattr(self, "_preset_lab_window", None)
        if window is not None and window.isVisible():
            window.raise_()
            window.activateWindow()
            return
        controller = self._backend_controller
        if controller is None:
            return
        window = PresetLabDialog(
            self,
            engine=controller.preset_engine,
            registry=controller.dsp_module_registry,
            state=controller.micro_custom_fx_state(),
            apply_callback=controller.set_micro_custom_fx_state,
            library_changed_callback=self._refresh_custom_voice_fx_library,
        )
        self._preset_lab_window = window
        window.destroyed.connect(self._clear_preset_lab_window)
        window.show()

    def _refresh_custom_voice_fx_library(self) -> None:
        page = getattr(self, "voice_fx_page", None)
        refresh = getattr(page, "refresh_custom_presets", None)
        if callable(refresh):
            refresh()
            page.sync_custom_state(self._backend_controller.micro_custom_fx_state())
        detached = getattr(self, "_detached_voice_fx_window", None)
        surface = getattr(detached, "surface", None) if detached is not None else None
        refresh_detached = getattr(surface, "refresh_custom_presets", None)
        if callable(refresh_detached):
            refresh_detached()
            surface.sync_custom_state(self._backend_controller.micro_custom_fx_state())

    def _clear_preset_lab_window(self, *_args) -> None:
        self._preset_lab_window = None

    def sync_custom_fx_state(self) -> None:
        if self._backend_controller is None:
            return
        state = self._backend_controller.micro_custom_fx_state()
        window = getattr(self, "_preset_lab_window", None)
        if window is not None:
            sync = getattr(window, "sync_state", None)
            if callable(sync):
                sync(state)
        page = getattr(self, "voice_fx_page", None)
        sync_page = getattr(page, "sync_custom_state", None)
        if callable(sync_page):
            sync_page(state)
        detached = getattr(self, "_detached_voice_fx_window", None)
        surface = getattr(detached, "surface", None) if detached is not None else None
        sync_detached = getattr(surface, "sync_custom_state", None)
        if callable(sync_detached):
            sync_detached(state)
        self._sync_voice_fx_monitoring(self._backend_controller.micro_voice_fx_state())

    def sync_micro_processing_state(self) -> None:
        page = getattr(self, "tune_page", None)
        if page is None or self._backend_controller is None:
            return
        sync = getattr(page, "sync_state", None)
        if callable(sync):
            sync(self._backend_controller.micro_processing_state())

    def sync_voice_fx_state(self) -> None:
        page = getattr(self, "voice_fx_page", None)
        if page is None or self._backend_controller is None:
            return
        state = self._backend_controller.micro_voice_fx_state()
        sync = getattr(page, "sync_state", None)
        if callable(sync):
            sync(state)
        self._sync_voice_fx_monitoring(state)
        detached = getattr(self, "_detached_voice_fx_window", None)
        if detached is not None and detached.isVisible():
            detached.sync_state()


    def _settings_page(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(2, 4, 2, 4)
        root.setSpacing(7)

        title = QLabel("Settings")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        settings = getattr(self._backend_controller, "settings", None)

        modules_title = QLabel("Modules")
        modules_title.setObjectName("settingsSectionTitle")
        modules_title.setStyleSheet("font-weight: 850; margin-top: 2px;")
        root.addWidget(modules_title)

        modules_hint = QLabel(
            "Install, enable and restart modules independently. "
            "Runtime health is checked before each live transition is accepted."
        )
        modules_hint.setObjectName("muted")
        modules_hint.setWordWrap(True)
        root.addWidget(modules_hint)

        self.module_checks: dict[str, QCheckBox] = {}
        self.module_state_labels: dict[str, QLabel] = {}
        self.module_cards: dict[str, QFrame] = {}
        self.module_uninstall_buttons: dict[str, QPushButton] = {}
        self.module_restart_buttons: dict[str, QPushButton] = {}

        install_button = QPushButton(
            "Install / update .ksmodule…"
        )
        install_button.setObjectName("padTopButton")
        install_button.clicked.connect(
            self._choose_module_package
        )
        root.addWidget(install_button)

        registry = self._module_registry
        manager = self._module_manager
        module_order = (
            "eq", "micro_tune", "hard_gate",
            "voice_fx", "pads", "remote"
        )

        if registry is not None:
            module_order += tuple(
                item.module_id
                for item in registry.manifests()
                if not item.builtin
            )

        for module_id in module_order:
            manifest = registry.manifest(module_id) if registry is not None else None
            if manifest is None:
                continue

            card = QFrame()
            card.setObjectName("settingsModuleCard")
            card.setStyleSheet(
                "QFrame#settingsModuleCard { border: 1px solid rgba(255,255,255,28); "
                "border-radius: 9px; background: rgba(255,255,255,7); }"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 7, 9, 7)
            card_layout.setSpacing(4)

            top_row = QHBoxLayout()
            top_row.setSpacing(8)
            name_label = QLabel(manifest.name)
            name_label.setStyleSheet("font-weight: 760;")
            name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            top_row.addWidget(name_label, 1)

            check = QCheckBox("Enabled")
            check.setChecked(bool(registry.is_enabled(module_id)))
            check.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            check.setToolTip(
                "Enable or disable this installed module. K-Sounds waits for the affected "
                "runtime health check before reporting the transition complete."
            )
            check.toggled.connect(
                lambda checked, mid=module_id: self._set_module_enabled(mid, bool(checked))
            )
            self.module_checks[module_id] = check
            top_row.addWidget(check, 0, Qt.AlignRight | Qt.AlignVCenter)
            card_layout.addLayout(top_row)

            state_label = QLabel()
            state_label.setObjectName("muted")
            state_label.setWordWrap(True)
            state_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.module_state_labels[module_id] = state_label
            card_layout.addWidget(state_label)

            actions_row = QHBoxLayout()
            actions_row.setSpacing(6)
            actions_row.addStretch(1)

            restart = QPushButton("Restart" if manifest.runtimes else "Reload")
            restart.setObjectName("padTopButton")
            restart.setMinimumWidth(74)
            restart.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            restart.setToolTip(
                "Restart this module's runtime without restarting K-Sounds."
                if manifest.runtimes
                else "Hot-reload this module's current processing state."
            )
            restart.clicked.connect(
                lambda _checked=False, mid=module_id: self._restart_module_runtime(mid)
            )
            self.module_restart_buttons[module_id] = restart
            actions_row.addWidget(restart)

            uninstall = QPushButton("Uninstall")
            uninstall.setObjectName("padTopButton")
            uninstall.setMinimumWidth(82)
            uninstall.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            uninstall.setVisible(not manifest.builtin and bool(manifest.removable))
            uninstall.clicked.connect(
                lambda _checked=False, mid=module_id: self._uninstall_module(mid)
            )
            self.module_uninstall_buttons[module_id] = uninstall
            actions_row.addWidget(uninstall)
            card_layout.addLayout(actions_row)

            self.module_cards[module_id] = card

            root.addWidget(card)
            self._refresh_module_state_row(module_id)

        extensions = getattr(
            self._backend_controller,
            "extension_registry",
            None
        )

        self._external_settings_specs = (
            extensions.active_surfaces("settings")
            if extensions is not None
            else ()
        )

        if self._external_settings_specs:
            title = QLabel("Module preferences")
            title.setObjectName("settingsSectionTitle")
            root.addWidget(title)

            for surface in self._external_settings_specs:
                frame = QFrame()
                frame.setObjectName("settingsModuleCard")

                frame.setStyleSheet(
                    "QFrame#settingsModuleCard { "
                    "border: 1px solid rgba(255,255,255,28); "
                    "border-radius: 9px; "
                    "background: rgba(255,255,255,7); }"
                )

                panel = QVBoxLayout(frame)
                panel.setContentsMargins(10, 7, 9, 7)

                panel.addWidget(
                    self._external_surface_widget(surface)
                )

                root.addWidget(frame)

        self.show_meters_check = QCheckBox("Show meters")
        self.show_meters_check.setChecked(bool(getattr(settings, "visualizer_enabled", True)))
        self.show_meters_check.toggled.connect(lambda checked: self._notify_visual("meters", bool(checked)))
        root.addWidget(self.show_meters_check)

        self.show_overlay_check = QCheckBox("Show overlay")
        self.show_overlay_check.setChecked(bool(getattr(settings, "overlay_enabled", False)))
        self.show_overlay_check.toggled.connect(lambda checked: self._notify_visual("overlay", bool(checked)))
        root.addWidget(self.show_overlay_check)

        self.close_to_tray_check = QCheckBox("Close to tray")
        self.close_to_tray_check.setChecked(bool(getattr(settings, "close_to_tray", True)))
        self.close_to_tray_check.toggled.connect(lambda checked: self._notify_visual("close_to_tray", bool(checked)))
        root.addWidget(self.close_to_tray_check)

        self.skip_exit_confirmation_check = QCheckBox("Don't ask before quitting")
        self.skip_exit_confirmation_check.setChecked(
            bool(getattr(settings, "skip_exit_confirmation", False))
        )
        self.skip_exit_confirmation_check.setToolTip(
            "When Close to tray is off, quit immediately without asking for confirmation."
        )
        self.skip_exit_confirmation_check.setEnabled(
            not self.close_to_tray_check.isChecked()
        )
        self.close_to_tray_check.toggled.connect(
            lambda checked: self.skip_exit_confirmation_check.setEnabled(not bool(checked))
        )
        self.skip_exit_confirmation_check.toggled.connect(
            lambda checked: self._notify_visual("skip_exit_confirmation", bool(checked))
        )
        root.addWidget(self.skip_exit_confirmation_check)

        self.autostart_check = QCheckBox("Start K-Sounds Hub automatically")
        self.autostart_check.setToolTip(
            "Create or remove the current user's XDG autostart entry. "
            "This setting is disabled on a fresh installation."
        )
        self._sync_autostart_check()
        self.autostart_check.toggled.connect(self._toggle_autostart)
        root.addWidget(self.autostart_check)

        self.auto_sort_apps_check = QCheckBox("Auto-sort new apps by PipeWire role")
        self.auto_sort_apps_check.setChecked(bool(getattr(settings, "auto_sort_app_streams_by_role", True)))
        self.auto_sort_apps_check.setToolTip(
            "Automatically route new unknown audio streams by PipeWire media.role. Manual app routes always win."
        )
        self.auto_sort_apps_check.toggled.connect(
            lambda checked: self._notify_visual("auto_sort_app_streams_by_role", bool(checked))
        )
        root.addWidget(self.auto_sort_apps_check)

        self.background_enabled_check = QCheckBox("Use custom background")
        self.background_enabled_check.setChecked(bool(getattr(settings, "wallpaper_enabled", False)))
        self.background_enabled_check.toggled.connect(lambda checked: self._notify_visual("background_enabled", bool(checked)))
        root.addWidget(self.background_enabled_check)

        bg_row = QHBoxLayout()
        choose_bg = QPushButton("Change background")
        choose_bg.setObjectName("padTopButton")
        choose_bg.clicked.connect(self._choose_background)
        bg_row.addWidget(choose_bg)

        wallpaper_path = str(getattr(settings, "wallpaper_path", "") or "")
        self.background_path_label = QLabel(Path(wallpaper_path).name if wallpaper_path else "Default background")
        self.background_path_label.setObjectName("muted")
        self.background_path_label.setWordWrap(True)
        bg_row.addWidget(self.background_path_label, 1)
        root.addLayout(bg_row)

        def add_settings_section(section_title: str) -> None:
            if root.count() > 0:
                root.addSpacing(4)
            label = QLabel(section_title)
            label.setObjectName("sectionTitle")
            root.addWidget(label)

        def add_settings_slider(label_text: str, key: str, value: int, *, live: bool = True) -> None:
            label = QLabel(label_text)
            label.setContentsMargins(0, 1, 0, 0)
            root.addWidget(label)
            slider = NoWheelSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(max(0, min(100, int(value))))
            slider.setMinimumHeight(28)
            slider.setMaximumHeight(28)
            slider.setTracking(bool(live))

            if live:
                slider.valueChanged.connect(
                    lambda new_value, item_key=key: self._notify_visual(item_key, int(new_value))
                )
            else:
                # NoWheelSlider calls setValue() itself while dragging, so
                # QSlider tracking=False alone is not sufficient here.
                # Keep the handle responsive but commit heavy visuals only
                # once the pointer is released.
                def commit_if_not_dragging(new_value, sl=slider, item_key=key):
                    if not sl.isSliderDown():
                        self._notify_visual(item_key, int(new_value))

                def commit_on_release(sl=slider, item_key=key):
                    self._notify_visual(item_key, int(sl.value()))

                slider.valueChanged.connect(commit_if_not_dragging)
                slider.sliderReleased.connect(commit_on_release)

            self._visual_sliders[key] = slider
            root.addWidget(slider)

        add_settings_section("Shortcuts")
        shortcut_label = QLabel("A/B MICRO Processing")
        shortcut_label.setToolTip("Toggle the MICRO Processing master anywhere inside K-Sounds. Clear the shortcut to disable it.")
        root.addWidget(shortcut_label)
        self.micro_processing_ab_shortcut_enabled_check = VisibleCheckBox("Enable in K-Sounds")
        self.micro_processing_ab_shortcut_enabled_check.setChecked(
            bool(getattr(settings, "micro_processing_ab_shortcut_enabled", True))
        )
        self.micro_processing_ab_shortcut_enabled_check.setToolTip(
            "Enable or disable this action inside K-Sounds. The assigned global key is "
            "managed automatically in the background."
        )
        root.addWidget(self.micro_processing_ab_shortcut_enabled_check)

        self.micro_processing_ab_kde_status = QLabel("Checking global shortcut…")
        self.micro_processing_ab_kde_status.setObjectName("muted")
        self.micro_processing_ab_kde_status.setWordWrap(True)
        root.addWidget(self.micro_processing_ab_kde_status)

        self.micro_processing_ab_shortcut_edit = ShortcutKeyEdit()
        self.micro_processing_ab_shortcut_edit.setShortcutText(
            str(getattr(settings, "micro_processing_ab_shortcut", "Ctrl+B") or "")
        )
        self.micro_processing_ab_shortcut_edit.setToolTip(
            "Click here and press one shortcut combination. K-Sounds registers it "
            "globally automatically."
        )
        self.micro_processing_ab_shortcut_edit.shortcutCommitted.connect(
            lambda trigger: self._notify_visual(
                "micro_processing_ab_shortcut",
                trigger,
            )
        )
        self.micro_processing_ab_shortcut_edit.setEnabled(True)
        self.micro_processing_ab_shortcut_enabled_check.toggled.connect(
            lambda checked: self._notify_visual(
                "micro_processing_ab_shortcut_enabled",
                bool(checked),
            )
        )
        root.addWidget(self.micro_processing_ab_shortcut_edit)


        add_settings_section("Wallpaper image")
        add_settings_slider("Saturation", "saturation", int(getattr(settings, "glass_background_saturation", 50)), live=False)
        add_settings_slider("Contrast", "contrast", int(getattr(settings, "glass_background_contrast", 50)), live=False)

        add_settings_section("Backdrop / overlay")
        add_settings_slider("Blur", "blur", int(getattr(settings, "glass_background_blur", 18)))
        add_settings_slider(
            "Darkness",
            "darkness",
            int(getattr(settings, "glass_background_darkness", 55)),
            live=False,
        )

        add_settings_section("UI / Glass")
        add_settings_slider(
            "Black glass opacity",
            "glass",
            int(getattr(settings, "glass_opacity", 70)),
            live=False,
        )

        root.addStretch(1)
        scroll = self._make_scroll_page(content)
        scroll.set_content_width_locked(True)
        return scroll

    def _module_state_text(self, module_id: str) -> str:
        manager = self._module_manager
        registry = self._module_registry
        if manager is None or registry is None:
            return "Unavailable"
        status = manager.status(module_id)
        manifest = registry.manifest(module_id)
        source = "bundled" if manifest is not None and manifest.builtin else "module package"

        if manifest is not None and not manifest.builtin:
            if status.phase == ModulePhase.ERROR:
                phase = f"Error · {status.detail}"
            elif status.runtime_state == "running":
                phase = "Running · handshake OK"
            elif not status.enabled:
                phase = "Disabled · external runner available"
            else:
                phase = status.phase.value.replace("_", " ").title()

        elif not status.enabled:
            disabled_detail = {
                "eq": "Disabled · EQ processing bypassed",
                "micro_tune": "Disabled · Tune DSP bypassed · shared MICRO transport may remain",
                "voice_fx": "Disabled · VFX DSP bypassed · shared MICRO transport may remain",
                "pads": "Disabled · Soundboard runtime stopped",
                "remote": "Disabled · server stopped · watchdog asleep",
            }.get(status.module_id, "Disabled")
            phase = disabled_detail
        elif status.module_id == "remote" and status.phase.value == "idle":
            phase = "Idle · server stopped · watchdog asleep"
        elif status.runtime_state == "running" and status.phase.value == "running":
            phase = "Running · health OK"
        else:
            phase = status.phase.value.replace("_", " ").title()

        controller = self._backend_controller
        getter = getattr(controller, "module_restart_count", None) if controller is not None else None
        restart_count = int(getter(status.module_id)) if callable(getter) else 0
        if restart_count > 0:
            phase = f"{phase} · Restarted #{restart_count}"
        version_text = f"v{status.version}"
        if status.loaded_version and status.loaded_version != status.version:
            version_text += f" · loaded v{status.loaded_version}"
        if status.update_available and status.catalog_version:
            version_text += f" → v{status.catalog_version} available"
        return f"Installed · {source} · {version_text} · {phase}"

    def _refresh_module_state_row(self, module_id: str) -> None:
        wanted = str(module_id or "").strip().lower()
        label = getattr(self, "module_state_labels", {}).get(wanted)
        if label is not None:
            label.setText(self._module_state_text(wanted))
        manager = self._module_manager
        registry = self._module_registry
        check = getattr(self, "module_checks", {}).get(wanted)

        manifest = (
            registry.manifest(wanted)
            if registry is not None
            else None
        )

        if check is not None and registry is not None:
            check.blockSignals(True)
            check.setChecked(bool(registry.is_enabled(wanted)))
            check.setEnabled(
                (manifest is None or manifest.builtin
                 or (self._backend_controller is not None
                     and self._backend_controller.external_module_supported(wanted)))
                and (
                    manager is None
                    or manager.status(wanted).phase.value not in {
                        "starting",
                        "stopping",
                        "updating",
                        "uninstalling",
                    }
                )
            )
            check.blockSignals(False)
        button = getattr(self, "module_uninstall_buttons", {}).get(wanted)
        if button is not None and manager is not None:
            status = manager.status(wanted)

            busy = status.phase.value in {
                "starting",
                "stopping",
                "installing",
                "updating",
                "uninstalling",
            }

            allowed = bool(
                manifest is not None
                and not manifest.builtin
                and manifest.removable
                and status.installed
                and not registry.enabled_dependents(wanted)
                and not busy
            )

            button.setEnabled(allowed)

            button.setToolTip(
                "Disable and stop the module, then uninstall it"
                if allowed and status.enabled else
                "Uninstall this disabled module"
                if allowed else
                "Wait for the current operation or disable dependent modules"
            )
        restart = getattr(self, "module_restart_buttons", {}).get(wanted)
        if restart is not None and manager is not None:
            status = manager.status(wanted)
            busy = status.phase.value in {"starting", "stopping", "updating", "uninstalling"}
            manifest = registry.manifest(wanted) if registry is not None else None
            idle_text = "Restart" if manifest is not None and (manifest.runtimes or not manifest.builtin) else "Reload"
            restart.setText("Working…" if busy else idle_text)
            restart.setEnabled(bool(status.installed and status.enabled) and not busy)

    def _set_module_enabled(self, module_id: str, enabled: bool) -> None:
        callback = self._module_callback
        manifest = self._module_registry.manifest(module_id) if self._module_registry else None
        if enabled and manifest is not None and not manifest.builtin:
            answer = QMessageBox.question(
                self, "Run external module?",
                f"{manifest.name} will run Python code on your computer. "
                "The separate process is NOT a security sandbox. Run only modules you trust.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._refresh_module_state_row(module_id)
                return
        ok = bool(callback(module_id, enabled)) if callable(callback) else False
        check = getattr(self, "module_checks", {}).get(str(module_id))
        if not ok and check is not None:
            check.blockSignals(True)
            check.setChecked(not bool(enabled))
            check.blockSignals(False)
        self._refresh_module_state_row(module_id)
        # Pads/VFX change whether the one shared Remote runtime is required.
        if str(module_id) in {"pads", "voice_fx", "remote"}:
            self._refresh_module_state_row("remote")

    def _restart_module_runtime(self, module_id: str) -> None:
        controller = self._backend_controller
        callback = getattr(controller, "restart_module_runtime", None) if controller is not None else None
        if callable(callback):
            callback(module_id)
        self._refresh_module_state_row(module_id)

    def _rebuild_module_settings(self) -> None:
        old = self.stack.widget(self._page_indices["settings"])
        was_current = self.stack.currentWidget() is old

        for surface in getattr(
            self,
            "_external_settings_specs",
            ()
        ):
            self._external_action_status_labels.pop(
                surface.route,
                None
            )

        self.stack.removeWidget(old)
        old.deleteLater()

        self._page_indices = {
            str(
                getattr(
                    self.stack.widget(i),
                    "_ksounds_route"
                )
            ): i
            for i in range(self.stack.count())
            if getattr(
                self.stack.widget(i),
                "_ksounds_route",
                None
            )
        }

        replacement = self._settings_page()
        self._add_page("settings", replacement)

        if was_current:
            self.stack.setCurrentWidget(replacement)

    def _choose_module_package(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Install K-Sounds module",
            str(Path.home() / "Downloads"),
            "K-Sounds module (*.ksmodule);;All files (*)",
        )

        if not path:
            return

        controller = self._backend_controller

        if (
            controller is None
            or not controller.install_module_archive(path)
        ):
            QMessageBox.warning(
                self,
                "Module package",
                "Package rejected; see K-Sounds status."
            )
            return

        self._rebuild_module_settings()

    def _perform_module_uninstall(self, module_id: str) -> None:
        controller = self._backend_controller

        callback = (
            getattr(controller, "uninstall_module", None)
            if controller is not None
            else None
        )

        if callable(callback) and callback(module_id):
            self._rebuild_module_settings()
            return

        self._refresh_module_state_row(module_id)

    def _uninstall_module(self, module_id: str) -> None:
        controller = self._backend_controller
        registry = self._module_registry
        manager = self._module_manager

        if (
            controller is None
            or registry is None
            or manager is None
        ):
            return

        manifest = registry.manifest(module_id)

        if (
            manifest is None
            or manifest.builtin
            or not manifest.removable
        ):
            return

        status = manager.status(module_id)

        if status.phase.value in {
            "starting",
            "stopping",
            "installing",
            "updating",
            "uninstalling",
        }:
            controller.status_changed.emit(
                f"Module {module_id}: operation in progress"
            )
            self._refresh_module_state_row(module_id)
            return

        if registry.is_enabled(module_id):
            answer = QMessageBox.question(
                self,
                "Disable and uninstall module?",
                f"Stop and uninstall {manifest.name}? "
                "Its module package will be removed.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if answer != QMessageBox.Yes:
                return

            pending = getattr(
                self,
                "_pending_module_uninstalls",
                None
            )

            if pending is None:
                pending = set()
                self._pending_module_uninstalls = pending

            pending.add(module_id)

            if not controller.set_module_enabled(
                module_id,
                False
            ):
                pending.discard(module_id)

                controller.status_changed.emit(
                    f"Module {module_id}: "
                    "could not disable before uninstall"
                )

            self._refresh_module_state_row(module_id)
            return

        if controller.external_module_host.running(module_id):
            controller.status_changed.emit(
                f"Module {module_id}: "
                "still running; uninstall refused"
            )
            return

        self._perform_module_uninstall(module_id)

    def _sync_autostart_check(self) -> None:
        checkbox = getattr(self, "autostart_check", None)
        if checkbox is None:
            return
        checkbox.blockSignals(True)
        checkbox.setChecked(autostart_enabled())
        checkbox.blockSignals(False)

    def _toggle_autostart(self, checked: bool) -> None:
        try:
            active = set_autostart_enabled(bool(checked))
        except Exception as exc:
            self._sync_autostart_check()
            controller = self._backend_controller
            if controller is not None:
                controller.status_changed.emit(f"Autostart update error — {exc}")
            return

        self._sync_autostart_check()
        controller = self._backend_controller
        if controller is not None:
            state = "enabled" if active else "disabled"
            controller.status_changed.emit(f"K-Sounds autostart {state}")

    def _choose_background(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose background",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*)",
        )
        if not path:
            return

        self.background_path_label.setText(Path(path).name)
        self.background_enabled_check.blockSignals(True)
        self.background_enabled_check.setChecked(True)
        self.background_enabled_check.blockSignals(False)
        self._notify_visual("background_path", path)


    def set_remote_status(self, state: str) -> None:
        label = getattr(self, "remote_status_label", None)
        if label is None:
            return

        normalized = str(state or "Offline").strip().lower()
        if normalized == "online":
            text = "Remote: Server online"
            style = (
                "color: rgba(205, 250, 235, 250);"
                "background: rgba(18, 82, 68, 130);"
                "border: 1px solid rgba(80, 210, 170, 125);"
            )
            label.setToolTip(
                "The local Remote service is healthy. For an explicit LAN/firewall "
                "diagnosis, run scripts/configure_soundboard_remote_firewall.sh --check."
            )
        elif normalized == "recovering":
            text = "Remote: Recovering"
            style = (
                "color: rgba(255, 235, 188, 250);"
                "background: rgba(96, 70, 22, 135);"
                "border: 1px solid rgba(220, 170, 70, 135);"
            )
        elif normalized == "checking":
            text = "Remote: Checking"
            style = (
                "color: rgba(200, 228, 245, 245);"
                "background: rgba(18, 52, 72, 130);"
                "border: 1px solid rgba(90, 170, 210, 120);"
            )
        elif normalized == "idle":
            text = "Remote: Idle"
            style = (
                "color: rgba(190, 205, 218, 225);"
                "background: rgba(30, 36, 44, 130);"
                "border: 1px solid rgba(115, 130, 145, 105);"
            )
            label.setToolTip("Remote installed but no enabled Pads/VFX surface requires the server")
        else:
            text = "Remote: Offline"
            style = (
                "color: rgba(255, 208, 214, 250);"
                "background: rgba(92, 35, 46, 135);"
                "border: 1px solid rgba(215, 105, 125, 130);"
            )

        if normalized != "online":
            label.setToolTip("Soundboard Remote runtime status")

        label.setText(text)
        label.setStyleSheet(
            style
            + "border-radius: 8px;"
            + "padding: 3px 8px;"
            + "font-size: 10px;"
            + "font-weight: 700;"
        )

    def _pads_page(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("sectionHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title = QLabel("Soundboard")
        title.setObjectName("sectionTitle")
        header_layout.addWidget(title)

        self.remote_status_label = QLabel("Remote: Checking")
        self.remote_status_label.setObjectName("remoteStatusBadge")
        self.remote_status_label.setToolTip("Soundboard Remote runtime status")
        self.remote_status_label.setStyleSheet(
            "color: rgba(190, 220, 238, 225);"
            "background: rgba(18, 42, 57, 125);"
            "border: 1px solid rgba(90, 160, 195, 105);"
            "border-radius: 8px;"
            "padding: 3px 8px;"
            "font-size: 10px;"
            "font-weight: 700;"
        )
        header_layout.addWidget(self.remote_status_label)
        header_layout.addStretch(1)

        self.pads_panel = PadsPanel(detach_callback=self._detach_pads, columns=3, backend_controller=self._backend_controller)

        self.pads_pair_button = QPushButton("📡  Remote")
        self.pads_pair_button.setObjectName("padTopButton")
        self.pads_pair_button.setCursor(Qt.PointingHandCursor)
        self.pads_pair_button.setToolTip("Pair Android remote")
        self.pads_pair_button.clicked.connect(self.pads_panel._pair_android_remote)
        self.pads_pair_button.setVisible(self._module_is_enabled("remote"))
        header_layout.addWidget(self.pads_pair_button, 0, Qt.AlignRight)

        root.addWidget(header)
        root.addWidget(self.pads_panel, 1)

        return content

    def _detach_pads(self) -> None:
        window = getattr(self, "_detached_pads_window", None)
        if window is not None and window.isVisible():
            window.raise_()
            window.activateWindow()
            return

        window = DetachedPadsWindow(
            backend_controller=self._backend_controller,
        )
        self._detached_pads_window = window
        window.destroyed.connect(self._clear_detached_pads_window)
        window.show()

    def _clear_detached_pads_window(self, *_args) -> None:
        self._detached_pads_window = None

    def _remove_module_page(self, route: str) -> None:
        wanted = str(route or "").strip()
        index = self._page_indices.get(wanted)
        if index is None:
            return
        widget = self.stack.widget(index)
        if widget is None:
            self._page_indices.pop(wanted, None)
            return
        if self.stack.currentWidget() is widget:
            apps_index = self._page_indices.get("apps", 0)
            self.stack.setCurrentIndex(apps_index)
        self.stack.removeWidget(widget)
        widget.deleteLater()

        self._external_action_status_labels.pop(
            wanted,
            None
        )

        self._page_indices = {}

        for candidate in range(self.stack.count()):
            item = self.stack.widget(candidate)
            key = str(getattr(item, "_ksounds_route", "") or "").strip()
            if key:
                self._page_indices[key] = candidate

    def _add_dynamic_page(self, route: str, widget: QWidget) -> None:
        setattr(widget, "_ksounds_route", str(route))
        self._page_indices[str(route)] = self.stack.addWidget(widget)

    def _register_external_control(
        self,
        surface,
        control,
        widget,
    ):
        self._external_control_widgets[
            (
                surface.module_id,
                surface.surface_id,
                control.control_id,
            )
        ] = widget

        with open("/tmp/ksounds_external_register.txt", "a") as f:
            f.write(
                f"{surface.module_id} {surface.surface_id} "
                f"{control.control_id} {type(widget).__name__}\n"
            )

        print(
            "REGISTER EXTERNAL:",
            surface.module_id,
            surface.surface_id,
            control.control_id,
            type(widget).__name__,
            flush=True,
        )

    def sync_external_module_state(
        self,
        module_id: str,
    ):
        controller = self._backend_controller

        values = controller._copy_confirmed_module_values()

        module_state = values.get(module_id, {})

        with open("/tmp/ksounds_sync_debug.txt", "a") as f:
            f.write(
                "SYNC " + module_id + " "
                + repr(module_state)
                + " widgets="
                + repr(list(self._external_control_widgets.keys()))
                + "\n"
            )

        for (mid, sid, cid), widget in self._external_control_widgets.items():
            if mid != module_id:
                continue

            value = (
                module_state
                .get(sid, {})
                .get(cid)
            )

            if value is None:
                continue

            blocked = widget.blockSignals(True)

            try:
                if isinstance(widget, QSlider):
                    with open("/tmp/ksounds_slider_apply.txt", "a") as f:
                        f.write(
                            f"APPLY slider {mid}:{sid}:{cid} "
                            f"old={widget.value()} new={int(value)}\\n"
                        )
                    widget.setValue(int(value))

                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))

                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)

            finally:
                widget.blockSignals(blocked)

    def _external_surface_widget(
        self,
        surface: ExtensionSurface
    ) -> QWidget:
        """Shared Desktop/Settings controls."""
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(10)
        title = QLabel(surface.label)
        title.setTextFormat(Qt.PlainText)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        controller = self._backend_controller

        feedback = QLabel("")
        feedback.setTextFormat(Qt.PlainText)
        feedback.setObjectName("muted")
        feedback.setWordWrap(True)

        self._external_action_status_labels[
            surface.route
        ] = feedback

        for control in surface.controls:
            if control.kind == "label":
                label = QLabel(control.label)
                label.setTextFormat(Qt.PlainText)
                label.setObjectName("muted")
                label.setWordWrap(True)
                layout.addWidget(label)
                continue
            def send(value, item=control):
                if controller is None:
                    feedback.setText("Unavailable")
                    return

                accepted = controller.invoke_module_ui_action(
                    surface.module_id,
                    surface.surface_id,
                    item.control_id,
                    item.action,
                    value,
                )

                feedback.setText(
                    "Sending…" if accepted
                    else "Unavailable"
                )
            if control.kind == "button":
                widget = QPushButton(control.label)
                widget.setObjectName("padTopButton")
                widget.setCursor(Qt.PointingHandCursor)

                widget.setStyleSheet(
                    "QPushButton:pressed { "
                    "background: rgba(62,216,255,65); "
                    "padding-top: 2px; }"
                )

                widget.clicked.connect(
                    lambda _checked=False, callback=send:
                    callback(True)
                )
                self._register_external_control(surface, control, widget)
                layout.addWidget(widget)
            elif control.kind == "toggle":
                widget = QCheckBox(control.label)
                widget.setChecked(bool(control.initial))
                widget.toggled.connect(send)
                self._register_external_control(surface, control, widget)
                layout.addWidget(widget)
            elif control.kind == "slider":
                layout.addWidget(QLabel(control.label))
                widget = QSlider(Qt.Horizontal)
                widget.setRange(control.minimum, control.maximum)
                widget.setValue(int(control.initial))
                widget.valueChanged.connect(
                    lambda value, callback=send: callback(value)
                )
                self._register_external_control(surface, control, widget)
                layout.addWidget(widget)
            elif control.kind == "select":
                layout.addWidget(QLabel(control.label))
                widget = QComboBox()
                for label, value in control.options:
                    widget.addItem(label, value)
                widget.setCurrentIndex(next(
                    (i for i, (_, value) in enumerate(control.options)
                     if value == control.initial), 0
                ))
                widget.currentIndexChanged.connect(
                    lambda index, combo=widget, callback=send:
                    callback(combo.itemData(index)) if index >= 0 else None
                )
                self._register_external_control(surface, control, widget)
                layout.addWidget(widget)
        layout.addWidget(feedback)
        layout.addStretch(1)
        return content

    def _external_page(
        self,
        surface: ExtensionSurface
    ) -> QWidget:
        return self._make_scroll_page(
            self._external_surface_widget(surface)
        )

    def _on_external_action_result(
        self,
        module_id: str,
        surface_id: str,
        control_id: str,
        ok: bool,
        message: str
    ) -> None:
        route = f"ext:{module_id}:{surface_id}"

        label = self._external_action_status_labels.get(
            route
        )

        if label is not None:
            label.setText(
                f"{control_id}: "
                f"{'✓' if ok else '⚠'} "
                f"{message[:180]}"
            )

    def refresh_external_pages(self) -> None:
        """Apply the live extension snapshot, removing stale widgets first."""
        registry = getattr(self._backend_controller, "extension_registry", None)
        surfaces = () if registry is None else registry.active_surfaces("desktop")
        active = {surface.route: surface for surface in surfaces}
        for route in tuple(self._page_indices):
            if route.startswith("ext:") and (route not in active or
                    self._external_page_specs.get(route) != active[route]):
                self._remove_module_page(route)
                self._external_page_specs.pop(route, None)
        for route, surface in active.items():
            if route not in self._page_indices:
                self._add_dynamic_page(route, self._external_page(surface))
                self._external_page_specs[route] = surface

    def refresh_module_pages(self, changed_module: str = "") -> None:
        """Add/remove module pages live after a successful runtime transition."""
        changed = str(changed_module or "").strip().lower()
        self.refresh_external_pages()
        builders = {
            "eq": self._eq_page,
            "micro_tune": self._tune_page,
            "voice_fx": self._voice_fx_page,
            "pads": self._pads_page,
        }
        for route, builder in builders.items():
            enabled = self._module_is_enabled(route)
            present = route in self._page_indices
            if enabled and not present:
                self._add_dynamic_page(route, builder())
            elif not enabled and present:
                if route == "pads":
                    window = getattr(self, "_detached_pads_window", None)
                    if window is not None:
                        try:
                            window.close()
                        except Exception:
                            pass
                    self.pads_panel = None
                    self.pads_pair_button = None
                elif route == "voice_fx":
                    window = getattr(self, "_detached_voice_fx_window", None)
                    if window is not None:
                        try:
                            window.close()
                        except Exception:
                            pass
                    lab = getattr(self, "_preset_lab_window", None)
                    if lab is not None:
                        try:
                            lab.close()
                        except Exception:
                            pass
                    self._preset_lab_window = None
                    self.voice_fx_page = None
                    self.voice_fx_pair_button = None
                elif route == "micro_tune":
                    self.tune_page = None
                self._remove_module_page(route)

        # Pair controls are views of the shared Remote capability, not separate
        # Remote instances. They appear/disappear without rebuilding the app.
        remote_enabled = self._module_is_enabled("remote")
        pair = getattr(self, "pads_pair_button", None)
        if pair is not None:
            pair.setVisible(remote_enabled and self._module_is_enabled("pads"))
        vfx_pair = getattr(self, "voice_fx_pair_button", None)
        if vfx_pair is not None:
            vfx_pair.setVisible(remote_enabled and self._module_is_enabled("voice_fx"))

        for module_id in ("eq", "micro_tune", "hard_gate", "voice_fx", "pads", "remote"):
            self._refresh_module_state_row(module_id)

    def show_page(self, page) -> None:
        # IP14 routes pages by module/core ID rather than fragile numeric
        # indices. Integer support remains for older callers/tests.
        if isinstance(page, int):
            legacy = ("apps", "eq", "micro_tune", "voice_fx", "pads", "settings")
            route = legacy[page] if 0 <= page < len(legacy) else "apps"
        else:
            route = str(page or "apps")
        index = self._page_indices.get(route)
        if index is None:
            index = self._page_indices.get("apps", 0)
            route = "apps"
        self.stack.setCurrentIndex(index)

        if route == "micro_tune":
            self.sync_micro_processing_state()
        elif route == "voice_fx":
            self.sync_voice_fx_state()
        elif route == "settings":
            self._sync_autostart_check()
        elif route == "pads":
            panel = getattr(self, "pads_panel", None)
            refresh = getattr(panel, "_sync_compact_soundboard_dashboard", None)
            if callable(refresh):
                QTimer.singleShot(0, refresh)


class PreviewWindow(QMainWindow):
    resize_margin = 9


    def _glass_activation_file(self):
        return Path.home() / ".cache" / "k-sounds-hub" / "glass.activate"

    def _close_to_tray_enabled(self) -> bool:
        try:
            backend = getattr(self, "backend_controller", None)
            settings = getattr(backend, "settings", None)
            if settings is not None:
                return bool(getattr(settings, "close_to_tray", False))
        except Exception:
            pass

        try:
            path = Path.home() / ".config" / "k-sounds-hub" / "settings.json"
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return bool(data.get("close_to_tray", False))
        except Exception:
            pass

        return False

    def _skip_exit_confirmation_enabled(self) -> bool:
        try:
            backend = getattr(self, "backend_controller", None)
            settings = getattr(backend, "settings", None)
            if settings is not None:
                return bool(getattr(settings, "skip_exit_confirmation", False))
        except Exception:
            pass

        try:
            if SETTINGS_PATH.is_file():
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                return bool(data.get("skip_exit_confirmation", False))
        except Exception:
            pass

        return False

    def _should_confirm_window_exit(self) -> bool:
        return (
            not self._allow_real_close
            and not self._close_to_tray_enabled()
            and not self._skip_exit_confirmation_enabled()
        )

    def _persist_skip_exit_confirmation(self) -> None:
        try:
            backend = getattr(self, "backend_controller", None)
            settings = getattr(backend, "settings", None)
            store = getattr(backend, "settings_store", None)
            if settings is None:
                return

            settings.skip_exit_confirmation = True

            checkbox = getattr(
                getattr(self, "drawer", None),
                "skip_exit_confirmation_check",
                None,
            )
            if checkbox is not None:
                checkbox.blockSignals(True)
                checkbox.setChecked(True)
                checkbox.blockSignals(False)

            save = getattr(store, "save", None)
            if callable(save):
                save(settings)
        except Exception:
            pass

    def _confirm_window_exit(self) -> bool:
        # Follow the standard Qt/KDE prompt structure. QMessageBox owns the
        # internal layout; do not reposition its private labels/grid manually.
        box = QMessageBox(self)
        box.setObjectName("glassExitConfirmation")
        box.setWindowTitle("Quit K-Sounds Hub")
        box.setIcon(QMessageBox.NoIcon)
        box.setText("Quit K-Sounds Hub?")
        box.setInformativeText("Are you sure you want to quit?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.setEscapeButton(QMessageBox.No)

        dont_ask = QCheckBox("Don't ask again")
        dont_ask.setObjectName("exitConfirmationCheck")
        box.setCheckBox(dont_ask)

        yes_button = box.button(QMessageBox.Yes)
        no_button = box.button(QMessageBox.No)
        if yes_button is not None:
            yes_button.setObjectName("exitYesButton")
            yes_button.setText("Yes")
        if no_button is not None:
            no_button.setObjectName("exitNoButton")
            no_button.setText("No")

        box.setStyleSheet("""
QMessageBox#glassExitConfirmation {
    background: rgba(8, 15, 24, 248);
}
QMessageBox#glassExitConfirmation QLabel#qt_msgbox_label {
    color: rgba(244, 250, 255, 250);
    font-size: 13px;
    font-weight: 750;
    padding: 0px;
}
QMessageBox#glassExitConfirmation QLabel#qt_msgbox_informativelabel {
    color: rgba(210, 228, 240, 235);
    font-size: 11px;
    padding: 0px;
}
QCheckBox#exitConfirmationCheck {
    color: rgba(210, 230, 243, 238);
    font-size: 11px;
    spacing: 8px;
}
QCheckBox#exitConfirmationCheck::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid rgba(140, 210, 245, 205);
    background: rgba(3, 10, 17, 245);
}
QCheckBox#exitConfirmationCheck::indicator:hover {
    border: 1px solid rgba(180, 235, 255, 245);
    background: rgba(27, 73, 101, 175);
}
QCheckBox#exitConfirmationCheck::indicator:checked {
    border: 1px solid rgba(190, 239, 255, 255);
    background: rgba(55, 170, 225, 245);
}
QMessageBox#glassExitConfirmation QPushButton {
    min-width: 76px;
    min-height: 28px;
    border-radius: 9px;
    color: rgba(242, 249, 253, 250);
    font-size: 12px;
    font-weight: 750;
    padding: 1px 12px;
}
/* Quitting is the consequential action: subtle negative/destructive tint. */
QPushButton#exitYesButton {
    border: 1px solid rgba(215, 125, 135, 130);
    background: rgba(122, 55, 64, 100);
}
QPushButton#exitYesButton:hover {
    border: 1px solid rgba(235, 145, 155, 185);
    background: rgba(150, 67, 78, 140);
}
/* Staying in the app is the safe action: neutral Glass/cyan tint. */
QPushButton#exitNoButton {
    border: 1px solid rgba(105, 190, 225, 125);
    background: rgba(35, 88, 118, 105);
}
QPushButton#exitNoButton:hover {
    border: 1px solid rgba(135, 215, 245, 180);
    background: rgba(45, 115, 150, 145);
}
QPushButton#exitNoButton:default {
    border: 1px solid rgba(150, 220, 250, 165);
    background: rgba(42, 105, 140, 125);
}
""")

        accepted = box.exec() == QMessageBox.Yes
        if accepted and dont_ask.isChecked():
            self._persist_skip_exit_confirmation()
        return accepted

    def _set_tray_visible_for_state(self) -> None:
        try:
            tray = getattr(self, "tray_icon", None)
            if tray is not None:
                tray.setVisible(self._close_to_tray_enabled())
        except Exception:
            pass

    def _focus_existing_window(self) -> None:
        try:
            if self.isMinimized():
                self.showNormal()
            else:
                self.show()
        except Exception:
            pass

        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _setup_external_activation(self) -> None:
        try:
            path = self._glass_activation_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

            watcher = QFileSystemWatcher([str(path)], self)
            watcher.fileChanged.connect(self._handle_external_activation)
            self._activation_watcher = watcher
        except Exception:
            pass

    def _handle_external_activation(self, _path: str = "") -> None:
        try:
            path = self._glass_activation_file()
            path.touch(exist_ok=True)

            watcher = getattr(self, "_activation_watcher", None)
            if watcher is not None and str(path) not in watcher.files():
                watcher.addPath(str(path))
        except Exception:
            pass

        self._focus_existing_window()

    def _save_detached_pads_window_geometry(self) -> None:
        try:
            drawer = getattr(self, "drawer", None)
            window = getattr(drawer, "_detached_pads_window", None)
            if window is None:
                return

            save = getattr(window, "_save_geometry_now", None)
            if callable(save):
                save()
        except Exception:
            pass

    def _save_all_window_geometry_now(self) -> None:
        self._save_geometry_now()
        self._save_detached_pads_window_geometry()

    def _shutdown_window_runtime(self) -> None:
        # Multiple normal exit paths can converge here (tray Quit, closeEvent,
        # QApplication.aboutToQuit). Latch before teardown so cleanup is safe
        # and deterministic even when Qt emits more than one exit signal.
        if getattr(self, "_window_runtime_shutdown", False):
            return
        self._window_runtime_shutdown = True

        # Stop meter polling before audio teardown. A late QTimer tick can call
        # meter_levels(), which lazily recreates the native meter after it was
        # already stopped, leaving an orphan in KDE/systemd's app scope.
        meter_timer = getattr(self, "meter_timer", None)
        if meter_timer is not None:
            try:
                meter_timer.stop()
            except Exception:
                pass

        self._save_all_window_geometry_now()

        try:
            tray = getattr(self, "tray_icon", None)
            if tray is not None:
                tray.hide()
        except Exception:
            pass

        try:
            overlay = getattr(self, "overlay", None)
            if overlay is not None:
                self._stop_overlay_renderer()
                shutdown = getattr(overlay, "shutdown", None)
                if callable(shutdown):
                    shutdown()
                else:
                    overlay.close()
        except Exception:
            pass

        try:
            backend = getattr(self, "backend_controller", None)
            if backend is not None:
                if bool(getattr(self, "_preserve_audio_routing_on_exit", False)):
                    backend.shutdown(preserve_audio_routing=True)
                else:
                    # Full exit keeps the historical teardown invariant: the
                    # shutdown guard and meter timer are latched/stopped first,
                    # then the destructive backend shutdown runs exactly once.
                    backend.shutdown()
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self._save_all_window_geometry_now()

        if (not self._allow_real_close) and self._close_to_tray_enabled():
            try:
                self.hide()
                tray = getattr(self, "tray_icon", None)
                if tray is not None:
                    tray.show()
            except Exception:
                pass
            event.ignore()
            return

        if (not self._ui_restart_in_progress) and self._should_confirm_window_exit():
            if not self._confirm_window_exit():
                event.ignore()
                return

        self._shutdown_window_runtime()
        event.accept()

        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def changeEvent(self, event) -> None:
        # Normal minimize: never force-show the window here.
        try:
            super().changeEvent(event)
        except Exception:
            pass

        try:
            if event.type() == QEvent.WindowStateChange:
                self._set_tray_visible_for_state()
        except Exception:
            pass

    def __init__(self):
        super().__init__()
        self.setWindowTitle("K-Sounds Hub")
        self._geometry_settings_key = "glass_hub_geometry"
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(550)
        self._geometry_save_timer.timeout.connect(self._save_geometry_now)
        # KSH_WINDOW_POLICY_DEDUPED
        QTimer.singleShot(0, self._setup_external_activation)
        QTimer.singleShot(300, self._set_tray_visible_for_state)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setMinimumSize(860, 430)
        _restore_window_geometry(self, self._geometry_settings_key, 1320, 560, 860, 430)

        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))

        self._allow_real_close = False
        self._window_runtime_shutdown = False
        self._preserve_audio_routing_on_exit = False
        self._ui_restart_in_progress = False
        self.tray_icon: QSystemTrayIcon | None = None

        self._background_source = QPixmap(str(APP_BG)) if APP_BG.is_file() else QPixmap()
        self._background_scaled_cache = QPixmap()
        self._background_saturation = 1.0
        self._background_contrast = 1.0
        self._background_darkness = 130
        self._glass_opacity = 178
        self._settings_sync_mtime_ns = 0
        self.backend_controller = GlassBackendController(self)
        # Bind the Module Registry once the backend exists. Channel-card callbacks
        # below are capability-gated through this local reference during startup.
        registry = self.backend_controller.module_registry
        self.backend_controller.restore_requested.connect(self._focus_existing_window)
        self.backend_controller.ui_restart_requested.connect(self._exit_for_preserved_handoff)
        self.backend_controller.micro_voice_fx_state_changed.connect(self._sync_micro_voice_fx_card_state)
        self._micro_processing_dialog = None
        self._voice_fx_dialog = None
        self.shortcut_registry = ShortcutRegistry()
        self.shortcut_manager = ShortcutManager(self.shortcut_registry)
        self._soundboard_shortcut_slots: dict[str, str] = {}
        # PadsPanel (including the detached Soundboard) talks to the central
        # registry through GlassBackendController so there is never a second
        # per-window shortcut state.
        self.backend_controller._soundboard_shortcut_state_resolver = self._soundboard_pad_shortcut_state
        self.backend_controller.soundboard_shortcut_edit_requested.connect(
            self._update_soundboard_pad_shortcut
        )
        self._register_core_shortcuts()
        self._micro_processing_ab_sequence = QKeySequence()
        self._refresh_micro_processing_ab_shortcut()
        self._global_tune_shortcut = None
        self._global_tune_shortcut_active = False
        self._global_tune_shortcut_starting = False
        self._global_mixer_shortcuts: dict[str, object] = {}
        QTimer.singleShot(0, self._start_global_shortcuts)
        self._load_visual_settings_from_backend()
        self.backend_controller.channel_state_changed.connect(self._sync_channel_card_from_backend)
        self._device_watch_signature: tuple = ()
        self._device_watch_shutting_down = False

        self.overlay = OverlayManager(self)
        self.overlay.set_enabled(bool(getattr(self.backend_controller.settings, "overlay_enabled", False)))
        self._hud_watchdog_timer = QTimer(self)
        self._hud_watchdog_timer.setInterval(2500)
        self._hud_watchdog_timer.timeout.connect(self._start_overlay_renderer_if_enabled)
        self._sync_overlay_watchdog()
        self.backend_controller.overlay_message_requested.connect(self._show_overlay_message)
        self._setup_tray_icon()

        self.channel_cards: list[ChannelCard] = []

        self._background_refresh_timer = QTimer(self)
        self._background_refresh_timer.setSingleShot(True)
        self._background_refresh_timer.setInterval(120)
        self._background_refresh_timer.timeout.connect(self._refresh_background_now)

        self._visual_style_timer = QTimer(self)
        self._visual_style_timer.setSingleShot(True)
        self._visual_style_timer.setInterval(24)
        self._visual_style_timer.timeout.connect(self._apply_visual_style)

        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)

        stack = QStackedLayout(central)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setStackingMode(QStackedLayout.StackAll)

        self.background_label = StaticBlurLabel()
        self.background_label.setObjectName("backgroundImage")
        self.background_label.setAlignment(Qt.AlignCenter)
        self.background_label.setScaledContents(True)
        self.background_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.background_blur = QGraphicsBlurEffect(self.background_label)
        self.background_blur.setBlurRadius(float(getattr(self.backend_controller.settings, "glass_background_blur", 18)))
        self.background_label.setGraphicsEffect(self.background_blur)

        self.background_wash = QFrame()
        self.background_wash.setObjectName("backgroundWash")
        self.background_wash.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.background_tint = QFrame()
        self.background_tint.setObjectName("backgroundTint")
        self.background_tint.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # Bake wallpaper + tint into one opaque cached widget by default.
        # KSH_GLASS_FLAT_BACKGROUND=0 restores the previous Qt widget stack.
        _flat_requested = os.environ.get("KSH_GLASS_FLAT_BACKGROUND", "1").strip() == "1"
        _flat_allowed = bool(getattr(self.background_label, "_ksh_static_blur_active", False))
        if _flat_requested and _flat_allowed:
            foreground = CachedGlassForeground(
                self.background_label,
                darkness_getter=lambda: self._background_darkness,
            )
            self._ksh_flat_foreground = foreground
            # The blur cache is built asynchronously after a blur-radius change.
            # Repaint the single cached backdrop AFTER that rebuild.
            self.background_label._ksh_deferred.timeout.connect(foreground.invalidate_background)
        else:
            foreground = QWidget()
            self._ksh_flat_foreground = None
        foreground.setObjectName("foreground")

        stack.addWidget(self.background_label)
        stack.addWidget(self.background_wash)
        stack.addWidget(self.background_tint)
        stack.addWidget(foreground)
        stack.setCurrentWidget(foreground)

        self.background_label.lower()
        self.background_wash.raise_()
        self.background_tint.raise_()
        foreground.raise_()

        outer = QVBoxLayout(foreground)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        content = QFrame()
        content.setObjectName("contentFrame")
        outer.addWidget(content, 1)

        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(9, 9, 9, 9)
        content_layout.setSpacing(10)

        self.nav_frame = QFrame()
        self.nav_frame.setObjectName("navRail")
        self.nav_frame.setFixedWidth(68)
        self.nav_layout = QVBoxLayout(self.nav_frame)
        self.nav_layout.setContentsMargins(6, 7, 6, 7)
        self.nav_layout.setSpacing(6)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        self._nav_routes: dict[int, str] = {}
        self._nav_button_by_route: dict[str, NavButton] = {}
        self._rebuild_navigation(preferred_route="mixer")

        content_layout.addWidget(self.nav_frame)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)
        cards_row.setContentsMargins(0, 0, 0, 0)

        for channel in CHANNELS:
            name, icon, devices, fallback_value, channel_key = channel
            value, muted = self._channel_state_for_card(channel_key, fallback_value)
            card_devices = self._channel_device_choices(channel_key, devices)
            hard_gate_available = channel_key == "micro" and registry.is_enabled("hard_gate")
            card = ChannelCard(
                name,
                icon,
                card_devices,
                value,
                channel_key,
                volume_callback=self._send_channel_volume,
                volume_live_callback=self._send_channel_volume_live,
                mute_callback=self._set_channel_muted,
                stereo_width=self._channel_stereo_width(channel_key),
                stereo_width_callback=self._set_channel_stereo_width,
                shortcut_state_getter=self._channel_shortcut_state,
                shortcut_change_callback=self._update_channel_shortcut,
                device_callback=self._set_channel_device,
                current_device=self._channel_device_label(channel_key, card_devices),
                device_favorite_getter=self._channel_device_is_favorite,
                device_favorite_callback=self._set_channel_device_favorite,
                hard_gate_available=hard_gate_available,
                hard_gate_enabled=self.backend_controller.micro_hard_gate_enabled() if hard_gate_available else False,
                hard_gate_threshold=self.backend_controller.micro_hard_gate_threshold() if hard_gate_available else 12,
                hard_gate_toggle_callback=self._set_micro_hard_gate_enabled if hard_gate_available else None,
                hard_gate_threshold_callback=self._set_micro_hard_gate_threshold if hard_gate_available else None,
                micro_processing_callback=self._open_micro_processing_dialog if (channel_key == "micro" and registry.is_enabled("micro_tune")) else None,
                voice_fx_callback=self._open_micro_voice_fx_dialog if (channel_key == "micro" and registry.is_enabled("voice_fx")) else None,
                voice_fx_active=(
                    self.backend_controller.micro_voice_fx_any_active()
                    if channel_key == "micro" and registry.is_enabled("voice_fx")
                    else False
                ),
            )
            card.sync_from_saved_state(
                volume=value,
                muted=muted,
                hard_gate_enabled=self.backend_controller.micro_hard_gate_enabled() if hard_gate_available else None,
                hard_gate_threshold=self.backend_controller.micro_hard_gate_threshold() if hard_gate_available else None,
            )
            self.channel_cards.append(card)
            cards_row.addWidget(card)

        content_layout.addLayout(cards_row, 1)
        self._set_meters_visible(bool(getattr(self.backend_controller.settings, "visualizer_enabled", True)))

        self.drawer = Drawer(
            self._apply_visual_setting,
            self.backend_controller,
            micro_eq_callback=self._open_micro_eq_editor,
            module_callback=self._set_module_enabled,
        )
        self._micro_processing_dialog = getattr(self.drawer, "tune_page", None)
        self._voice_fx_dialog = getattr(self.drawer, "voice_fx_page", None)
        self.backend_controller.remote_status_changed.connect(self.drawer.set_remote_status)
        self.backend_controller.micro_processing_state_changed.connect(self.drawer.sync_micro_processing_state)
        self.backend_controller.micro_voice_fx_state_changed.connect(
            lambda _active: self.drawer.sync_voice_fx_state()
        )
        self.backend_controller.micro_custom_fx_state_changed.connect(
            self.drawer.sync_custom_fx_state
        )
        self.backend_controller.module_state_changed.connect(self._on_module_state_changed)
        self.backend_controller.module_ui_state_changed.connect(
            self.drawer.sync_external_module_state
        )
        self.drawer.setVisible(False)
        content_layout.addWidget(self.drawer)
        QTimer.singleShot(0, self._sync_tune_shortcut_runtime_ui)

        self._install_resize_event_filter()
        self._apply_visual_style()
        self._refresh_background()
        self._start_meter_updates()
        self._device_watch_debounce = QTimer(self)
        self._device_watch_debounce.setSingleShot(True)
        self._device_watch_debounce.setInterval(300)
        self._device_watch_debounce.timeout.connect(lambda: self._refresh_device_selectors_if_needed(force=True))
        self._app_route_watch_debounce = QTimer(self)
        self._app_route_watch_debounce.setSingleShot(True)
        self._app_route_watch_debounce.setInterval(180)
        self._app_route_watch_debounce.timeout.connect(self._refresh_app_routes_after_stream_event)

        self._device_watch_process = QProcess(self)
        self._device_watch_process.setProgram("pactl")
        self._device_watch_process.setArguments(["subscribe"])
        self._device_watch_process.readyReadStandardOutput.connect(self._handle_device_watch_events)
        self._device_watch_process.finished.connect(self._handle_device_watch_finished)
        self._device_watch_process.errorOccurred.connect(
            lambda _error: self.backend_controller.status_changed.emit("Audio device watcher unavailable")
        )

        app = QApplication.instance()
        if app is not None:
            # Final ownership backstop: if Qt exits through any normal path,
            # tear down K-Sounds audio children before the application scope ends.
            app.aboutToQuit.connect(self._shutdown_window_runtime)
            app.aboutToQuit.connect(self._stop_device_watch_process)
            app.aboutToQuit.connect(self._stop_overlay_renderer)
            app.aboutToQuit.connect(self._stop_global_shortcuts)
        if __import__("os").environ.get("KSH_DEVICE_WATCHER_DISABLED", "").strip() != "1":
            QTimer.singleShot(0, self._start_device_watch_process)
            QTimer.singleShot(0, lambda: self._refresh_device_selectors_if_needed(force=True))
        QTimer.singleShot(450, self.backend_controller.normalize_channel_playback_routes)

        # Glass is the future K-Sounds frontend, not a companion window.
        # Do not poll the old/stable UI settings file to drive live controls:
        # it adds latency and makes the transition architecture wrong.
        self._settings_sync_timer = None

    def _install_resize_event_filter(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        def enable_tracking(widget: QWidget) -> None:
            widget.setMouseTracking(True)
            for child in widget.findChildren(QWidget):
                child.setMouseTracking(True)

        enable_tracking(self)

    def eventFilter(self, obj, event) -> bool:
        # Catch the configured chord at QApplication level instead of relying
        # on QShortcut routing through modal Glass dialogs.
        if event.type() == QEvent.KeyPress:
            try:
                auto_repeat = bool(event.isAutoRepeat())
            except Exception:
                auto_repeat = False
            if not auto_repeat and not isinstance(obj, QKeySequenceEdit):
                if self._micro_processing_ab_event_matches(event):
                    shortcut_id = "micro.tune.toggle"
                    if self.shortcut_manager.activated(shortcut_id):
                        self.shortcut_manager.deactivated(shortcut_id)
                    event.accept()
                    return True

        if not isinstance(obj, QWidget):
            return super().eventFilter(obj, event)

        owner_attr = getattr(obj, "window", None)
        try:
            owner = owner_attr() if callable(owner_attr) else owner_attr
        except TypeError:
            owner = None

        if owner is not self:
            return super().eventFilter(obj, event)

        if self.isMaximized():
            return super().eventFilter(obj, event)

        if event.type() == QEvent.MouseMove:
            edges = self._edges_for_global_pos(event.globalPosition().toPoint())
            self.setCursor(self._cursor_for_edges(edges))
            return super().eventFilter(obj, event)

        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            edges = self._edges_for_global_pos(event.globalPosition().toPoint())
            if edges:
                handle = self.windowHandle()
                edge_flags = Qt.Edges()
                if "left" in edges:
                    edge_flags |= Qt.LeftEdge
                if "right" in edges:
                    edge_flags |= Qt.RightEdge
                if "top" in edges:
                    edge_flags |= Qt.TopEdge
                if "bottom" in edges:
                    edge_flags |= Qt.BottomEdge

                if handle is not None:
                    try:
                        if handle.startSystemResize(edge_flags):
                            event.accept()
                            return True
                    except Exception:
                        pass

        return super().eventFilter(obj, event)

    def _setup_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = None
            return

        tray = QSystemTrayIcon(self)
        if APP_ICON.is_file():
            tray.setIcon(QIcon(str(APP_ICON)))
        elif not self.windowIcon().isNull():
            tray.setIcon(self.windowIcon())

        tray.setToolTip("K-Sounds Hub")

        menu = QMenu(self)

        show_action = menu.addAction("Show K-Sounds Hub")
        show_action.triggered.connect(self._restore_from_tray)

        restart_ui_action = menu.addAction("Restart UI (keep audio)")
        restart_ui_action.triggered.connect(self._restart_ui_preserve_audio)

        menu.addSeparator()
        quit_action = menu.addAction("Quit K-Sounds completely")
        quit_action.triggered.connect(self._quit_from_tray)

        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()

        self.tray_icon = tray

    def _tray_activated(self, reason) -> None:
        if reason in {QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick}:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        # A restored window returns to normal close-to-tray behavior even if a
        # previous explicit Quit path was interrupted before QApplication exit.
        self._allow_real_close = False

        if self.tray_icon is not None:
            try:
                self.tray_icon.show()
            except Exception:
                pass

        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_for_preserved_handoff(self) -> None:
        if self._ui_restart_in_progress:
            return
        self._ui_restart_in_progress = True
        self._preserve_audio_routing_on_exit = True
        self._allow_real_close = True
        self._save_all_window_geometry_now()
        self.close()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _restart_ui_preserve_audio(self) -> None:
        if self._ui_restart_in_progress:
            return

        helper = PROJECT_ROOT / "scripts" / "restart_ksound_ui_preserve_audio.sh"
        if not helper.is_file():
            self.backend_controller.status_changed.emit("UI restart helper is missing")
            return

        try:
            env = os.environ.copy()
            env["KSH_PRESERVE_AUDIO_ROUTING"] = "1"
            subprocess.Popen(
                [str(helper), "--after-pid", str(os.getpid())],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            self.backend_controller.status_changed.emit(f"Could not start UI handoff — {exc}")
            return

        self._exit_for_preserved_handoff()

    def _quit_from_tray(self) -> None:
        # Explicit full quit always tears down audio routing too. A previous
        # preserve-restart request must never leak into the user's Quit action.
        self._preserve_audio_routing_on_exit = False
        self._ui_restart_in_progress = False
        self._allow_real_close = True
        self._save_all_window_geometry_now()
        if self.tray_icon is not None:
            try:
                self.tray_icon.hide()
            except Exception:
                pass
        self.close()

        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _navigation_specs(self) -> list[tuple[str, str, str]]:
        registry = self.backend_controller.module_registry
        specs: list[tuple[str, str, str]] = [
            ("mixer", "▥", "Mixer"),
            ("apps", "▤", "Apps"),
        ]
        for manifest in registry.navigation_modules():
            # IP15m3 executes external workers, but cannot render arbitrary
            # external pages until the UI contribution API is implemented.
            if manifest.builtin:
                specs.append(
                    (
                        manifest.module_id,
                        manifest.nav_icon,
                        manifest.nav_label,
                    )
                )
        extensions = getattr(self.backend_controller, "extension_registry", None)
        drawer = getattr(self, "drawer", None)
        if extensions is not None and drawer is not None:
            pages = getattr(drawer, "_page_indices", {})
            for surface in extensions.active_surfaces("desktop"):
                if surface.route in pages:
                    specs.append((surface.route, surface.icon, surface.label))
        specs.append(("settings", "⚙", "Settings"))
        return specs

    def _rebuild_navigation(self, preferred_route: str | None = None) -> str:
        route = str(
            preferred_route
            or getattr(self, "_current_nav_route", "mixer")
            or "mixer"
        )
        layout = self.nav_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        old_group = getattr(self, "nav_group", None)
        if old_group is not None:
            try:
                old_group.deleteLater()
            except Exception:
                pass
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        self._nav_routes = {}
        self._nav_button_by_route = {}

        specs = self._navigation_specs()
        available = {item[0] for item in specs}
        if route not in available:
            route = "mixer"

        selected_id = 0
        for idx, (item_route, icon, label) in enumerate(specs):
            button = NavButton(icon, label)
            self.nav_buttons.append(button)
            self._nav_routes[idx] = item_route
            self._nav_button_by_route[item_route] = button
            self.nav_group.addButton(button, idx)
            layout.addWidget(button)
            if item_route == route:
                selected_id = idx
        layout.addStretch(1)
        self.nav_group.idClicked.connect(self._on_nav_clicked)
        selected = self.nav_group.button(selected_id)
        if selected is not None:
            selected.setChecked(True)
        self._current_nav_route = route
        return route

    def _on_module_state_changed(self, module_id: str) -> None:
        wanted = str(module_id or "").strip().lower()
        current_route = str(getattr(self, "_current_nav_route", "mixer") or "mixer")
        drawer = getattr(self, "drawer", None)
        if drawer is not None:
            drawer.refresh_module_pages(wanted)

            extensions = getattr(
                self.backend_controller,
                "extension_registry",
                None
            )

            new_settings = (
                extensions.active_surfaces("settings")
                if extensions is not None
                else ()
            )

            if new_settings != getattr(
                drawer,
                "_external_settings_specs",
                ()
            ):
                drawer._rebuild_module_settings()

            self._micro_processing_dialog = getattr(
                drawer,
                "tune_page",
                None
            )
            self._voice_fx_dialog = getattr(drawer, "voice_fx_page", None)

        registry = self.backend_controller.module_registry
        for card in getattr(self, "channel_cards", []):
            if str(getattr(card, "channel_key", "")) != "micro":
                continue
            hard_gate_enabled = registry.is_enabled("hard_gate")
            tune_enabled = registry.is_enabled("micro_tune")
            vfx_enabled = registry.is_enabled("voice_fx")
            card._hard_gate_available = bool(hard_gate_enabled)
            card.gate_btn.setVisible(bool(hard_gate_enabled))
            card._micro_processing_callback = self._open_micro_processing_dialog if tune_enabled else None
            card._voice_fx_callback = self._open_micro_voice_fx_dialog if vfx_enabled else None
            card.fx_btn.setVisible(bool(tune_enabled))
            card.voice_fx_btn.setVisible(bool(vfx_enabled))
            card.set_voice_fx_active(bool(vfx_enabled and self.backend_controller.micro_voice_fx_any_active()))
            card._position_gate_button()

        selected_route = self._rebuild_navigation(preferred_route=current_route)
        target_id = next(
            (idx for idx, route in self._nav_routes.items() if route == selected_route),
            0,
        )
        self._show_drawer_nav_page(target_id)

        if drawer is not None:
            drawer._refresh_module_state_row(wanted)

            pending = getattr(
                drawer,
                "_pending_module_uninstalls",
                set()
            )

            if wanted in pending:
                status = (
                    self.backend_controller
                    .module_manager.status(wanted)
                )

                busy = status.phase.value in {
                    "starting",
                    "stopping",
                    "installing",
                    "updating",
                    "uninstalling",
                }

                if not busy:
                    pending.discard(wanted)

                    manager = (
                        self.backend_controller
                        .module_manager
                    )

                    host = (
                        self.backend_controller
                        .external_module_host
                    )

                    if (
                        not registry.is_enabled(wanted)
                        and manager.can_uninstall(wanted)
                        and not host.running(wanted)
                    ):
                        QTimer.singleShot(
                            0,
                            lambda mid=wanted:
                                drawer._perform_module_uninstall(mid)
                        )
                    else:
                        self.backend_controller.status_changed.emit(
                            f"Module {wanted}: disable did not complete; "
                            "not uninstalled"
                        )

    def _show_drawer_nav_page(self, nav_id: int) -> None:
        wanted = int(nav_id)
        button = self.nav_group.button(wanted)
        if button is not None:
            button.setChecked(True)
        route = self._nav_routes.get(wanted, "mixer")
        self._current_nav_route = route
        if route == "mixer":
            self.drawer.setVisible(False)
            return
        self.drawer.setVisible(True)
        self.drawer.show_page(route)

    def _on_nav_clicked(self, idx: int) -> None:
        self._show_drawer_nav_page(idx)

    def _set_module_enabled(self, module_id: str, enabled: bool) -> bool:
        return bool(self.backend_controller.set_module_enabled(module_id, enabled))

    def _soundboard_shortcut_slot(self, slot_key: str) -> dict | None:
        wanted = str(slot_key or "").strip()
        if not wanted:
            return None
        try:
            slot = self.backend_controller.soundboard_service.slot_for_key(wanted)
        except Exception:
            return None
        return slot if isinstance(slot, dict) else None

    def _ensure_soundboard_shortcut_action(self, slot_key: str):
        wanted = str(slot_key or "").strip()
        if not wanted:
            return None

        shortcut_id = soundboard_pad_shortcut_id(wanted)
        existing = self.shortcut_registry.get(shortcut_id)
        if existing is not None:
            self._soundboard_shortcut_slots[shortcut_id] = wanted
            return existing

        slot = self._soundboard_shortcut_slot(wanted)
        if slot is None:
            return None

        label = str(slot.get("label") or "").strip()
        if not label:
            path_text = str(slot.get("path") or "").strip()
            label = Path(path_text).stem if path_text else wanted
        legacy_trigger = normalize_shortcut_trigger(str(slot.get("shortcut") or "").strip())

        action = self.shortcut_registry.register(
            ShortcutBinding(
                shortcut_id=shortcut_id,
                description=f"Soundboard — {label}",
                trigger=legacy_trigger,
                enabled=bool(legacy_trigger),
                module="soundboard",
                allow_hold=False,
            ),
            on_activate=lambda key=wanted: QTimer.singleShot(
                0, lambda: self._activate_soundboard_pad_shortcut(key)
            ),
        )
        self._soundboard_shortcut_slots[shortcut_id] = wanted
        return action

    def _register_soundboard_shortcuts(self) -> None:
        if not self.backend_controller.module_enabled("pads"):
            return
        try:
            data = self.backend_controller.soundboard_service.read_document()
        except Exception:
            data = {}
        slots = data.get("slots", []) if isinstance(data, dict) else []
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            slot_key = str(slot.get("id") or "").strip()
            if slot_key:
                self._ensure_soundboard_shortcut_action(slot_key)

    def _soundboard_pad_shortcut_state(self, slot_key: str) -> tuple[str, bool]:
        action = self._ensure_soundboard_shortcut_action(slot_key)
        if action is None:
            return "", False
        binding = action.binding
        clean = str(binding.trigger or "").strip()
        return clean, bool(binding.enabled and clean)

    def _activate_soundboard_pad_shortcut(self, slot_key: str) -> None:
        self.backend_controller.play_soundboard_slot_shortcut(slot_key)

    def _mirror_soundboard_shortcuts_to_document(self) -> None:
        try:
            data = self.backend_controller.soundboard_service.read_document()
        except Exception:
            return
        slots = data.get("slots", []) if isinstance(data, dict) else []
        if not isinstance(slots, list):
            return

        by_id = {
            str(slot.get("id") or "").strip(): slot
            for slot in slots
            if isinstance(slot, dict) and str(slot.get("id") or "").strip()
        }
        changed = False
        for shortcut_id, slot_key in tuple(self._soundboard_shortcut_slots.items()):
            action = self.shortcut_registry.get(shortcut_id)
            slot = by_id.get(slot_key)
            if action is None or slot is None:
                continue
            binding = action.binding
            desired = str(binding.trigger or "").strip() if binding.enabled else ""
            if str(slot.get("shortcut") or "").strip() != desired:
                slot["shortcut"] = desired
                changed = True
            if "shortcut_enabled" in slot:
                slot.pop("shortcut_enabled", None)
                changed = True
        if changed:
            try:
                self.backend_controller.soundboard_service.write_document(data)
            except Exception:
                pass

    def _sync_soundboard_shortcut_ui(self, shortcut_id: str) -> None:
        slot_key = self._soundboard_shortcut_slots.get(str(shortcut_id or "").strip(), "")
        action = self.shortcut_registry.get(str(shortcut_id or "").strip())
        if not slot_key or action is None:
            return
        binding = action.binding
        self.backend_controller.sync_soundboard_pad_shortcut_state(
            slot_key,
            str(binding.trigger or ""),
            bool(binding.enabled),
        )

    def _ensure_micro_eq_preset_shortcut_action(self, profile_name: str):
        name = str(profile_name or "").strip()
        if not name:
            return None
        shortcut_id = micro_eq_preset_shortcut_id(name)
        action = self.shortcut_registry.get(shortcut_id)
        if action is not None:
            return action
        self.shortcut_registry.register(
            ShortcutBinding(
                shortcut_id=shortcut_id,
                description=f"MICRO preset — {name}",
                trigger="",
                enabled=False,
                module="micro_preset",
                allow_hold=False,
            ),
            on_activate=lambda wanted=name: QTimer.singleShot(
                0, lambda: self._activate_micro_eq_preset_shortcut(wanted)
            ),
        )
        return self.shortcut_registry.get(shortcut_id)

    def _register_micro_eq_preset_shortcuts(self) -> None:
        if not self.backend_controller.module_enabled("eq"):
            return
        names, _selected = self.backend_controller.eq_profile_names("micro")
        for name in names:
            self._ensure_micro_eq_preset_shortcut_action(name)

    def _micro_eq_preset_shortcut_state(self, profile_name: str) -> tuple[str, bool]:
        action = self._ensure_micro_eq_preset_shortcut_action(profile_name)
        if action is None:
            return "", False
        binding = action.binding
        return str(binding.trigger or ""), bool(binding.enabled)

    def _activate_micro_eq_preset_shortcut(self, profile_name: str) -> None:
        wanted = str(profile_name or "").strip()
        if not wanted:
            return
        if self.backend_controller.select_eq_profile("micro", wanted):
            self.backend_controller.status_changed.emit(f"MICRO preset shortcut → {wanted}")

    def _update_micro_eq_preset_shortcut(
        self,
        profile_name: str,
        *,
        trigger: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        action = self._ensure_micro_eq_preset_shortcut_action(profile_name)
        if action is None:
            return
        shortcut_id = action.binding.shortcut_id
        binding = action.binding

        if trigger is not None:
            requested = str(trigger or "").strip()
            if requested and shortcut_trigger_is_reserved(requested):
                self.backend_controller.status_changed.emit(
                    "That key is reserved for shortcut editing and cannot be mapped"
                )
                return
            normalized = normalize_shortcut_trigger(requested)
            binding.trigger = normalized
            binding.enabled = bool(normalized)
            self._save_shortcut_registry_later()
            QTimer.singleShot(0, lambda sid=shortcut_id: self._restart_global_mixer_shortcut(sid))
            return

        if enabled is not None:
            requested_enabled = bool(enabled) and bool(str(binding.trigger or "").strip())
            backend = self.shortcut_manager.backend_state(shortcut_id)
            if requested_enabled and backend.authoritative and not backend.enabled:
                requested_enabled = False
            binding.enabled = requested_enabled
            self._save_shortcut_registry_later()

    def _register_core_shortcuts(self) -> None:
        settings = self.backend_controller.settings
        tune = ShortcutBinding(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO Processing (TUNE)",
            trigger=str(getattr(settings, "micro_processing_ab_shortcut", "Ctrl+B") or ""),
            enabled=bool(getattr(settings, "micro_processing_ab_shortcut_enabled", True)),
            module="micro_processing",
            allow_hold=False,
        )
        self.shortcut_registry.register(
            tune,
            on_activate=lambda: QTimer.singleShot(0, self._toggle_micro_processing_ab),
        )

        for _name, _icon, _devices, _fallback, channel_key in CHANNELS:
            channel_label = CHANNEL_SHORTCUT_LABELS.get(channel_key, channel_key.upper())
            for action, action_label in MIXER_SHORTCUT_ACTIONS:
                shortcut_id = mixer_shortcut_id(channel_key, action)
                hold_capable = action in {"volup", "voldown"}
                self.shortcut_registry.register(
                    ShortcutBinding(
                        shortcut_id=shortcut_id,
                        description=f"{channel_label} — {action_label}",
                        trigger="",
                        enabled=False,
                        module="mixer",
                        allow_hold=hold_capable,
                    ),
                    on_activate=lambda key=channel_key, mixer_action=action: QTimer.singleShot(
                        0, lambda: self._activate_mixer_shortcut(key, mixer_action)
                    ),
                    on_deactivate=(
                        (lambda key=channel_key, mixer_action=action: QTimer.singleShot(
                            0, lambda: self._deactivate_mixer_shortcut(key, mixer_action)
                        ))
                        if hold_capable else None
                    ),
                )

        # Soundboard pads and MICRO presets are dynamic product actions.
        self._register_soundboard_shortcuts()
        self._register_micro_eq_preset_shortcuts()

        saved = getattr(settings, "shortcut_bindings", [])
        if isinstance(saved, list):
            self.shortcut_registry.apply_saved_bindings(saved)
        self._mirror_soundboard_shortcuts_to_document()
        self._sync_tune_legacy_settings_from_registry()

    def _tune_shortcut_binding(self) -> ShortcutBinding:
        return self.shortcut_registry.require("micro.tune.toggle").binding

    def _mixer_shortcut_binding(self, channel_key: str, action: str) -> ShortcutBinding:
        return self.shortcut_registry.require(mixer_shortcut_id(channel_key, action)).binding

    def _channel_shortcut_state(self, channel_key: str, action: str) -> tuple[str, bool]:
        binding = self._mixer_shortcut_binding(channel_key, action)
        return str(binding.trigger or ""), bool(binding.enabled)

    def _mixer_volume_hold_state(self):
        if not hasattr(self, "_mixer_volume_held"):
            self._mixer_volume_held: set[tuple[str, str]] = set()
            self._mixer_volume_targets: dict[str, float] = {}
            self._mixer_volume_last_rendered: dict[str, int] = {}
            self._mixer_volume_started_at: dict[tuple[str, str], float] = {}
            self._mixer_volume_last_tick_at: dict[str, float] = {}
            self._mixer_volume_hold_timer = QTimer(self)
            self._mixer_volume_hold_timer.setInterval(16)
            self._mixer_volume_hold_timer.setTimerType(Qt.PreciseTimer)
            self._mixer_volume_hold_timer.timeout.connect(self._tick_mixer_volume_hold)
        return self._mixer_volume_held, self._mixer_volume_targets

    def _render_mixer_shortcut_volume(self, channel_key: str, value: int) -> None:
        """Paint only the shortcut-owned volume UI; no model/style resync."""
        target = max(0, min(100, int(value)))
        for card in getattr(self, "channel_cards", []):
            if str(getattr(card, "channel_key", "") or "").strip() == str(channel_key):
                renderer = getattr(card, "render_shortcut_volume", None)
                if callable(renderer):
                    renderer(target)
                else:
                    card.sync_from_saved_state(volume=target, animate_volume=False)
                break

    def _set_mixer_shortcut_volume_target(
        self,
        channel_key: str,
        value: int,
        *,
        show_overlay: bool = False,
        final: bool = False,
    ) -> bool:
        target = max(0, min(100, int(value)))
        self._render_mixer_shortcut_volume(channel_key, target)
        return bool(self.backend_controller._set_mixer_shortcut_volume_target(
            channel_key,
            target,
            show_overlay=show_overlay,
            final=final,
        ))

    def _activate_mixer_shortcut(self, channel_key: str, action: str) -> None:
        if action == "device_next":
            self._cycle_channel_favorite_device(channel_key)
            return
        if action not in {"mute", "volup", "voldown"}:
            return
        if action == "mute":
            self.backend_controller._apply_mixer_action(channel_key, action)
            return

        state = self.backend_controller.channel_state(channel_key)
        if state is None:
            return
        volume, muted = state
        if muted:
            # Preserve the established behavior and its mute overlay.
            self.backend_controller._apply_mixer_action(channel_key, action)
            return

        held, targets = self._mixer_volume_hold_state()
        token = (str(channel_key), str(action))
        now = time.monotonic()
        held.add(token)
        self._mixer_volume_started_at[token] = now
        self._mixer_volume_last_tick_at[channel_key] = now
        if channel_key not in targets:
            targets[channel_key] = float(max(0, min(100, int(volume))))

        # One tap is exactly ±5% and paints immediately. If the key remains
        # held, continuous motion starts shortly afterwards, but there is no
        # perceived hold delay because this first visual/audio step is instant.
        direction = 1.0 if action == "volup" else -1.0
        targets[channel_key] = max(0.0, min(100.0, targets[channel_key] + (5.0 * direction)))
        rendered = int(round(targets[channel_key]))
        self._mixer_volume_last_rendered[channel_key] = rendered
        self._set_mixer_shortcut_volume_target(channel_key, rendered, show_overlay=True)

        timer = self._mixer_volume_hold_timer
        if not timer.isActive():
            timer.start()

    def _tick_mixer_volume_hold(self) -> None:
        held, targets = self._mixer_volume_hold_state()
        if not held:
            self._mixer_volume_hold_timer.stop()
            return

        now = time.monotonic()
        directions: dict[str, int] = {}
        active_since: dict[str, float] = {}
        for channel_key, action in tuple(held):
            directions[channel_key] = directions.get(channel_key, 0) + (1 if action == "volup" else -1)
            stamp = self._mixer_volume_started_at.get((channel_key, action), now)
            active_since[channel_key] = min(active_since.get(channel_key, stamp), stamp)

        for channel_key, direction in directions.items():
            if direction == 0:
                self._mixer_volume_last_tick_at[channel_key] = now
                continue

            state = self.backend_controller.channel_state(channel_key)
            if state is None or bool(state[1]):
                self._mixer_volume_last_tick_at[channel_key] = now
                continue
            if channel_key not in targets:
                targets[channel_key] = float(state[0])

            # The immediate ±5 step above handles taps. Continuous motion begins
            # after a short 105 ms intent threshold, then follows elapsed time
            # rather than timer tick count. This keeps motion smooth even if one
            # frame is late and avoids desktop key-repeat entirely.
            if (now - active_since.get(channel_key, now)) < 0.105:
                self._mixer_volume_last_tick_at[channel_key] = now
                continue

            previous = self._mixer_volume_last_tick_at.get(channel_key, now)
            dt = max(0.0, min(0.020, now - previous))
            self._mixer_volume_last_tick_at[channel_key] = now
            if dt <= 0.0:
                continue

            targets[channel_key] = max(
                0.0,
                min(100.0, targets[channel_key] + (58.0 * dt * direction)),
            )
            rendered = int(round(targets[channel_key]))
            if rendered == self._mixer_volume_last_rendered.get(channel_key):
                continue

            self._mixer_volume_last_rendered[channel_key] = rendered

            # Overlay mirrors the exact slider target. OverlayManager is a
            # latest-value-wins background writer, so publishing every visible
            # integer step is cheap and never blocks the GUI thread.
            self._set_mixer_shortcut_volume_target(
                channel_key,
                rendered,
                show_overlay=True,
            )

    def _deactivate_mixer_shortcut(self, channel_key: str, action: str) -> None:
        held, targets = self._mixer_volume_hold_state()
        token = (str(channel_key), str(action))
        held.discard(token)
        self._mixer_volume_started_at.pop(token, None)

        # Finalize only when no volume direction remains held for this channel.
        channel_still_held = any(key == str(channel_key) for key, _action in held)
        if not channel_still_held:
            target = int(round(targets.pop(channel_key, float(
                (self.backend_controller.channel_state(channel_key) or (0, False))[0]
            ))))
            self._mixer_volume_last_rendered.pop(channel_key, None)
            self._mixer_volume_last_tick_at.pop(channel_key, None)
            self._set_mixer_shortcut_volume_target(channel_key, target, show_overlay=True, final=True)

        if not held and self._mixer_volume_hold_timer.isActive():
            self._mixer_volume_hold_timer.stop()

    def _sync_channel_shortcut_ui(self, shortcut_id: str = "") -> None:
        for card in getattr(self, "channel_cards", []):
            channel_key = str(getattr(card, "channel_key", "") or "").strip()
            if not channel_key:
                continue
            for action, _label in MIXER_SHORTCUT_ACTIONS:
                sid = mixer_shortcut_id(channel_key, action)
                if shortcut_id and sid != shortcut_id:
                    continue
                binding = self.shortcut_registry.require(sid).binding
                card.sync_shortcut_binding(action, binding.trigger, binding.enabled)

    def _sync_tune_legacy_settings_from_registry(self) -> None:
        binding = self._tune_shortcut_binding()
        settings = self.backend_controller.settings
        settings.micro_processing_ab_shortcut = str(binding.trigger or "")
        settings.micro_processing_ab_shortcut_enabled = bool(binding.enabled)
        settings.shortcut_bindings = self.shortcut_registry.export_bindings()

    def _save_shortcut_registry_later(self) -> None:
        self._sync_tune_legacy_settings_from_registry()
        self._save_visual_settings_later()

    def _sync_tune_shortcut_runtime_ui(self, state=None) -> None:
        drawer = getattr(self, "drawer", None)
        sync = getattr(drawer, "sync_shortcut_runtime", None)
        if not callable(sync):
            return
        if state is None:
            bridge = getattr(self, "_global_tune_shortcut", None)
            state = bridge.state() if bridge is not None else None
        if state is None:
            return
        sync(state, bool(self._tune_shortcut_binding().enabled))

    def _on_global_tune_shortcut_state(self, state) -> None:
        shortcut_id = "micro.tune.toggle"
        authoritative = bool(getattr(state, "portal_ready", False))
        registered = bool(getattr(state, "registered", False))
        kde_enabled = bool(getattr(state, "kde_enabled", False))
        kde_trigger = str(getattr(state, "kde_trigger", "") or "").strip()
        detail = str(getattr(state, "detail", "") or "").strip()

        self.shortcut_manager.set_backend_state(
            shortcut_id,
            authoritative=authoritative,
            registered=registered,
            enabled=kde_enabled,
            trigger_description=kde_trigger,
            detail=detail,
        )
        self._global_tune_shortcut_active = bool(authoritative and kde_enabled)

        binding = self._tune_shortcut_binding()
        settings_changed = False

        if authoritative:
            # KDE is the first authority. If KDE disables/removes the action,
            # K-Sounds must not keep its secondary switch enabled.
            if not kde_enabled:
                if binding.enabled:
                    binding.enabled = False
                    settings_changed = True
                if str(binding.trigger or ""):
                    binding.trigger = ""
                    settings_changed = True
                self._micro_processing_ab_sequence = QKeySequence()

            # Keep K-Sounds' saved/fallback key aligned with KDE when KDE gives
            # us a single human-readable trigger. Multiple KDE bindings remain
            # displayed by the runtime status without being collapsed here.
            if kde_enabled and kde_trigger and ", " not in kde_trigger:
                try:
                    sequence = QKeySequence(kde_trigger, QKeySequence.NativeText)
                    portable = sequence.toString(QKeySequence.PortableText)
                except Exception:
                    portable = ""
                if portable and portable != str(binding.trigger or ""):
                    binding.trigger = portable
                    settings_changed = True

        if settings_changed:
            self._sync_tune_legacy_settings_from_registry()
            self._save_visual_settings_later()

        self._sync_tune_shortcut_runtime_ui(state)

        if authoritative and "already used" in detail.lower():
            self.backend_controller.status_changed.emit(detail)
        elif authoritative and kde_enabled:
            self.backend_controller.status_changed.emit(
                f"Global TUNE shortcut available — {kde_trigger or 'assigned'}"
            )
        elif authoritative and registered:
            self.backend_controller.status_changed.emit(
                "Global TUNE shortcut cleared — K-Sounds switch forced off"
            )
        elif authoritative:
            self.backend_controller.status_changed.emit(
                "Global TUNE shortcut is not registered — choose a key in K-Sounds"
            )
        elif detail:
            self.backend_controller.status_changed.emit(
                f"Global TUNE backend unavailable; in-app fallback only — {detail}"
            )

    def _on_global_mixer_shortcut_state(self, shortcut_id: str, state) -> None:
        authoritative = bool(getattr(state, "portal_ready", False))
        registered = bool(getattr(state, "registered", False))
        kde_enabled = bool(getattr(state, "kde_enabled", False))
        kde_trigger = str(getattr(state, "kde_trigger", "") or "").strip()
        detail = str(getattr(state, "detail", "") or "").strip()

        self.shortcut_manager.set_backend_state(
            shortcut_id,
            authoritative=authoritative,
            registered=registered,
            enabled=kde_enabled,
            trigger_description=kde_trigger,
            detail=detail,
        )

        action = self.shortcut_registry.get(shortcut_id)
        if action is None:
            return
        binding = action.binding
        settings_changed = False

        if authoritative:
            if not kde_enabled:
                if binding.enabled:
                    binding.enabled = False
                    settings_changed = True
                if str(binding.trigger or ""):
                    binding.trigger = ""
                    settings_changed = True
            if kde_enabled and kde_trigger and ", " not in kde_trigger:
                try:
                    sequence = QKeySequence(kde_trigger, QKeySequence.NativeText)
                    portable = sequence.toString(QKeySequence.PortableText)
                except Exception:
                    portable = ""
                if portable and portable != str(binding.trigger or ""):
                    binding.trigger = portable
                    settings_changed = True

        if settings_changed:
            self._save_shortcut_registry_later()
        if binding.module == "soundboard":
            self._sync_soundboard_shortcut_ui(shortcut_id)
        else:
            self._sync_channel_shortcut_ui(shortcut_id)

        if authoritative and "already used" in detail.lower():
            self.backend_controller.status_changed.emit(detail)
        elif detail and ("error" in detail.lower() or "failed" in detail.lower()):
            self.backend_controller.status_changed.emit(detail)

    def _start_global_mixer_shortcuts(self) -> None:
        try:
            from .shortcuts_kglobalaccel import KdeGlobalShortcut, find_helper as find_kde_shortcut_helper
            from .shortcuts_portal_bind import XdgGlobalShortcut

            use_kde = find_kde_shortcut_helper() is not None
            bridges: dict[str, object] = {}
            for binding in self.shortcut_registry.bindings():
                if binding.module not in {"mixer", "soundboard", "micro_preset"}:
                    continue
                trigger = str(binding.trigger or "").strip()
                if not use_kde and not trigger:
                    continue
                bridge_class = KdeGlobalShortcut if use_kde else XdgGlobalShortcut
                sid = binding.shortcut_id
                bridge = bridge_class(
                    shortcut_id=sid,
                    description=binding.description,
                    on_activate=lambda shortcut=sid: QTimer.singleShot(
                        0, lambda: self.shortcut_manager.activated(shortcut)
                    ),
                    on_deactivate=lambda shortcut=sid: QTimer.singleShot(
                        0, lambda: self.shortcut_manager.deactivated(shortcut)
                    ),
                    on_state_change=lambda state, shortcut=sid: QTimer.singleShot(
                        0, lambda current=state: self._on_global_mixer_shortcut_state(shortcut, current)
                    ),
                )
                bridges[sid] = bridge
                bridge.start(trigger)
            self._global_mixer_shortcuts = bridges
        except Exception as exc:
            self.backend_controller.status_changed.emit(
                f"Global action shortcuts unavailable — {exc}"
            )

    def _start_global_shortcuts(self) -> None:
        self._start_global_tune_shortcut()
        self._start_global_mixer_shortcuts()

    def _stop_global_mixer_shortcuts(self) -> None:
        bridges = getattr(self, "_global_mixer_shortcuts", {})
        self._global_mixer_shortcuts = {}
        for shortcut_id, bridge in tuple(bridges.items()):
            try:
                bridge.stop()
            except Exception:
                pass
            self.shortcut_manager.set_backend_state(
                shortcut_id, authoritative=False, detail="global shortcut helper stopped"
            )
            try:
                self.shortcut_manager.deactivated(shortcut_id)
            except Exception:
                pass

    def _stop_global_shortcuts(self) -> None:
        self._stop_global_tune_shortcut()
        self._stop_global_mixer_shortcuts()
        try:
            from .shortcuts_kglobalaccel import stop_shared_helper
            stop_shared_helper()
        except Exception:
            pass

    def _restart_global_mixer_shortcut(self, shortcut_id: str) -> None:
        bridge = getattr(self, "_global_mixer_shortcuts", {}).get(shortcut_id)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        binding = self.shortcut_registry.require(shortcut_id).binding
        try:
            from .shortcuts_kglobalaccel import KdeGlobalShortcut, find_helper as find_kde_shortcut_helper
            if find_kde_shortcut_helper() is None:
                return
            new_bridge = KdeGlobalShortcut(
                shortcut_id=shortcut_id,
                description=binding.description,
                on_activate=lambda: QTimer.singleShot(
                    0, lambda: self.shortcut_manager.activated(shortcut_id)
                ),
                on_deactivate=lambda: QTimer.singleShot(
                    0, lambda: self.shortcut_manager.deactivated(shortcut_id)
                ),
                on_state_change=lambda state: QTimer.singleShot(
                    0, lambda current=state: self._on_global_mixer_shortcut_state(shortcut_id, current)
                ),
            )
            self._global_mixer_shortcuts[shortcut_id] = new_bridge
            new_bridge.start(binding.trigger)
        except Exception as exc:
            self.backend_controller.status_changed.emit(str(exc))

    def _update_channel_shortcut(
        self,
        channel_key: str,
        action: str,
        *,
        trigger: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        shortcut_id = mixer_shortcut_id(channel_key, action)
        shortcut_action = self.shortcut_registry.get(shortcut_id)
        if shortcut_action is None:
            return
        binding = shortcut_action.binding

        if trigger is not None:
            requested = str(trigger or "").strip()
            if requested and shortcut_trigger_is_reserved(requested):
                self.backend_controller.status_changed.emit(
                    "That key is reserved for shortcut editing and cannot be mapped"
                )
                self._sync_channel_shortcut_ui(shortcut_id)
                return
            normalized = normalize_shortcut_trigger(requested)
            binding.trigger = normalized
            binding.enabled = bool(normalized)

            bridge = getattr(self, "_global_mixer_shortcuts", {}).get(shortcut_id)
            backend = self.shortcut_manager.backend_state(shortcut_id)
            setter = getattr(bridge, "set_trigger", None)
            self._save_shortcut_registry_later()
            self._sync_channel_shortcut_ui(shortcut_id)

            if backend.authoritative and callable(setter):
                if not setter(normalized):
                    self.backend_controller.status_changed.emit(
                        "Could not update the global shortcut"
                    )
                    self._sync_channel_shortcut_ui(shortcut_id)
                return
            QTimer.singleShot(0, lambda sid=shortcut_id: self._restart_global_mixer_shortcut(sid))
            return

        if enabled is not None:
            requested_enabled = bool(enabled) and bool(str(binding.trigger or "").strip())
            backend = self.shortcut_manager.backend_state(shortcut_id)
            if requested_enabled and backend.authoritative and not backend.enabled:
                requested_enabled = False
            binding.enabled = requested_enabled
            self._save_shortcut_registry_later()
            self._sync_channel_shortcut_ui(shortcut_id)

    def _update_soundboard_pad_shortcut(
        self,
        slot_key: str,
        trigger=None,
        enabled=None,
    ) -> None:
        action = self._ensure_soundboard_shortcut_action(slot_key)
        if action is None:
            self.backend_controller.status_changed.emit("Soundboard shortcut: pad not found")
            return

        shortcut_id = action.binding.shortcut_id
        binding = action.binding

        if trigger is not None:
            requested = str(trigger or "").strip()
            if requested and shortcut_trigger_is_reserved(requested):
                self.backend_controller.status_changed.emit(
                    "That key is reserved for shortcut editing and cannot be mapped"
                )
                self._sync_soundboard_shortcut_ui(shortcut_id)
                return

            normalized = normalize_shortcut_trigger(requested)
            binding.trigger = normalized
            binding.enabled = bool(normalized)

            bridge = getattr(self, "_global_mixer_shortcuts", {}).get(shortcut_id)
            backend = self.shortcut_manager.backend_state(shortcut_id)
            setter = getattr(bridge, "set_trigger", None)
            self._save_shortcut_registry_later()
            self._sync_soundboard_shortcut_ui(shortcut_id)

            if backend.authoritative and callable(setter):
                if not setter(normalized):
                    self.backend_controller.status_changed.emit(
                        "Could not update the global pad shortcut"
                    )
                    self._sync_soundboard_shortcut_ui(shortcut_id)
                return

            QTimer.singleShot(
                0, lambda sid=shortcut_id: self._restart_global_mixer_shortcut(sid)
            )
            return

        if enabled is not None:
            requested_enabled = bool(enabled) and bool(str(binding.trigger or "").strip())
            backend = self.shortcut_manager.backend_state(shortcut_id)
            if requested_enabled and backend.authoritative and not backend.enabled:
                requested_enabled = False
            binding.enabled = requested_enabled
            self._save_shortcut_registry_later()
            self._sync_soundboard_shortcut_ui(shortcut_id)

    def _start_global_tune_shortcut(self) -> None:
        if not self.backend_controller.module_enabled("micro_tune"):
            self._global_tune_shortcut_active = False
            self.shortcut_manager.set_backend_state(
                "micro.tune.toggle",
                authoritative=False,
                detail="Micro Tune module disabled",
            )
            return
        if bool(getattr(self, "_global_tune_shortcut_starting", False)):
            return

        binding = self._tune_shortcut_binding()
        trigger = str(binding.trigger or "").strip()

        self._global_tune_shortcut_starting = True
        self._global_tune_shortcut_active = False
        try:
            from .shortcuts_kglobalaccel import KdeGlobalShortcut, find_helper as find_kde_shortcut_helper
            from .shortcuts_portal_bind import XdgGlobalShortcut

            previous = getattr(self, "_global_tune_shortcut", None)
            if previous is not None:
                previous.stop()

            bridge_class = KdeGlobalShortcut if find_kde_shortcut_helper() is not None else XdgGlobalShortcut
            bridge = bridge_class(
                shortcut_id=binding.shortcut_id,
                description=binding.description,
                on_activate=lambda: QTimer.singleShot(
                    0, lambda: self.shortcut_manager.activated(binding.shortcut_id)
                ),
                on_deactivate=lambda: QTimer.singleShot(
                    0, lambda: self.shortcut_manager.deactivated(binding.shortcut_id)
                ),
                on_state_change=lambda state: QTimer.singleShot(
                    0, lambda current=state: self._on_global_tune_shortcut_state(current)
                ),
            )
            self._global_tune_shortcut = bridge
            state = bridge.start(trigger)
            self.backend_controller.status_changed.emit(
                f"Checking global TUNE shortcut state — {state.detail}"
            )
        except Exception as exc:
            self._global_tune_shortcut_active = False
            self.shortcut_manager.set_backend_state(
                binding.shortcut_id,
                authoritative=False,
                detail=str(exc),
            )
            self.backend_controller.status_changed.emit(
                f"Global TUNE shortcut unavailable; using in-app fallback — {exc}"
            )
        finally:
            self._global_tune_shortcut_starting = False

    def _stop_global_tune_shortcut(self) -> None:
        self._global_tune_shortcut_active = False
        bridge = getattr(self, "_global_tune_shortcut", None)
        self._global_tune_shortcut = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        self.shortcut_manager.set_backend_state(
            "micro.tune.toggle",
            authoritative=False,
            detail="global shortcut helper stopped",
        )
        try:
            self.shortcut_manager.deactivated("micro.tune.toggle")
        except Exception:
            pass

    def _restart_global_tune_shortcut(self) -> None:
        if not hasattr(self, "_global_tune_shortcut"):
            return
        self._stop_global_tune_shortcut()
        QTimer.singleShot(0, self._start_global_tune_shortcut)

    def _configure_global_tune_shortcut(self) -> None:
        bridge = getattr(self, "_global_tune_shortcut", None)
        if bridge is None or not bool(getattr(bridge, "portal_ready", False)):
            self.backend_controller.status_changed.emit(
                "KDE shortcut configuration is not available right now"
            )
            return
        if not bridge.configure():
            self.backend_controller.status_changed.emit(
                "Could not open KDE shortcut configuration"
            )

    def _refresh_micro_processing_ab_shortcut(self, *, restart_global: bool = True) -> None:
        binding = self._tune_shortcut_binding()
        raw = str(binding.trigger or "").strip()
        self._micro_processing_ab_sequence = (
            QKeySequence(raw) if binding.enabled and raw else QKeySequence()
        )
        self._sync_tune_legacy_settings_from_registry()
        if restart_global and hasattr(self, "_global_tune_shortcut"):
            QTimer.singleShot(0, self._restart_global_tune_shortcut)
        else:
            self._sync_tune_shortcut_runtime_ui()

    def _micro_processing_ab_event_matches(self, event) -> bool:
        if not self.backend_controller.module_enabled("micro_tune"):
            return False
        bridge = getattr(self, "_global_tune_shortcut", None)
        # Once KDE/XDG is live it is authoritative even when KDE has disabled
        # the action. Never let the in-app fallback bypass a KDE OFF state.
        if bool(getattr(bridge, "portal_ready", False)):
            return False

        configured = getattr(self, "_micro_processing_ab_sequence", QKeySequence())
        if configured.isEmpty():
            return False

        # Qt 6 already exposes the exact key + modifiers combination. Building
        # a QKeySequence from event.key() | int(modifiers) is unreliable on
        # current PySide6 because the enum representation is not the legacy
        # integer bitmask expected by old Qt code.
        try:
            pressed = QKeySequence(event.keyCombination())
        except Exception:
            try:
                key = int(event.key())
                modifiers = int(event.modifiers().value)
            except Exception:
                try:
                    key = int(event.key())
                    modifiers = int(event.modifiers())
                except Exception:
                    return False
            pressed = QKeySequence(key | modifiers)

        wanted = configured.toString(QKeySequence.PortableText)
        actual = pressed.toString(QKeySequence.PortableText)
        return bool(wanted) and actual == wanted

    def _toggle_micro_processing_ab(self) -> None:
        if not self.backend_controller.module_enabled("micro_tune"):
            return
        dialog = getattr(self, "_micro_processing_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.master_toggle.setChecked(not dialog.master_toggle.isChecked())
            return

        state = self.backend_controller.micro_processing_state()
        enabled = bool(state.get("processing_enabled", False))
        self.backend_controller.set_micro_processing_state(
            {"processing_enabled": not enabled}
        )

    def _save_visual_settings_later(self) -> None:
        timer = getattr(self.backend_controller, "_save_timer", None)
        if timer is not None:
            timer.start()

    def _sync_shared_visual_settings(self, changed_key: str) -> None:
        drawer = getattr(self, "drawer", None)
        sync = getattr(drawer, "sync_visual_settings", None)
        if callable(sync):
            sync(self.backend_controller.settings, changed_key)

    def _load_background_source_from_settings(self) -> QPixmap:
        return _background_source_for_settings(self.backend_controller.settings)

    def _load_visual_settings_from_backend(self) -> None:
        settings = self.backend_controller.settings
        self._background_source = self._load_background_source_from_settings()
        self._background_scaled_cache = QPixmap()
        self._background_saturation = self._background_saturation_factor_from_slider(int(getattr(settings, "glass_background_saturation", 50)))
        self._background_contrast = self._background_contrast_factor_from_slider(int(getattr(settings, "glass_background_contrast", 50)))
        self._background_darkness = int(40 + max(0, min(100, int(getattr(settings, "glass_background_darkness", 55)))) * 2.0)
        self._glass_opacity = int(70 + max(0, min(100, int(getattr(settings, "glass_opacity", 70)))) * 1.55)

        if hasattr(self, "background_blur"):
            self.background_blur.setBlurRadius(float(max(0, min(100, int(getattr(settings, "glass_background_blur", 18))))))

        if hasattr(self, "overlay"):
            self.overlay.set_enabled(bool(getattr(settings, "overlay_enabled", False)))

        self._set_meters_visible(bool(getattr(settings, "visualizer_enabled", True)))

    def _set_meters_visible(self, visible: bool) -> None:
        enabled = bool(visible) and os.environ.get("KSH_METERS_DISABLED", "").strip() != "1"
        for card in getattr(self, "channel_cards", []):
            setter = getattr(card, "set_meters_visible", None)
            if callable(setter):
                setter(enabled)

        self.backend_controller.set_metering_enabled(enabled)

        timer = getattr(self, "meter_timer", None)
        if timer is not None:
            if enabled and not timer.isActive():
                timer.start()
            elif not enabled and timer.isActive():
                timer.stop()

    def _apply_visual_setting(self, key: str, value) -> None:
        settings = self.backend_controller.settings
        key = str(key or "").strip().lower()

        if key == "meters":
            settings.visualizer_enabled = bool(value)
            self._set_meters_visible(settings.visualizer_enabled)
            self._save_visual_settings_later()
            return

        if key == "overlay":
            settings.overlay_enabled = bool(value)
            self.overlay.set_enabled(settings.overlay_enabled)
            self._sync_overlay_watchdog()
            self._save_visual_settings_later()
            return

        if key == "close_to_tray":
            settings.close_to_tray = bool(value)
            self._set_tray_visible_for_state()
            self._save_visual_settings_later()
            return

        if key == "skip_exit_confirmation":
            settings.skip_exit_confirmation = bool(value)
            self._save_visual_settings_later()
            return

        if key == "auto_sort_app_streams_by_role":
            settings.auto_sort_app_streams_by_role = bool(value)
            try:
                self.backend_controller._save_timer.start()
            except Exception:
                try:
                    self.backend_controller.settings_store.save(settings)
                except Exception:
                    pass
            try:
                self.backend_controller._reapply_saved_app_routes()
            except Exception as exc:
                self.backend_controller.status_changed.emit(f"Apps auto-sort refresh error — {exc}")
            return

        if key == "micro_processing_ab_configure_kde":
            self._configure_global_tune_shortcut()
            return

        if key == "micro_processing_ab_shortcut_enabled":
            backend = self.shortcut_manager.backend_state("micro.tune.toggle")
            requested = bool(value)
            if requested and backend.authoritative and not backend.enabled:
                requested = False
                self.backend_controller.status_changed.emit(
                    "Assign a global shortcut before enabling this K-Sounds action"
                )
            self.shortcut_registry.set_binding(
                "micro.tune.toggle", enabled=requested
            )
            # The K-Sounds switch is secondary; toggling it must not tear down
            # or recreate KDE's authoritative portal session.
            self._refresh_micro_processing_ab_shortcut(restart_global=False)
            self._save_shortcut_registry_later()
            self._sync_shared_visual_settings(key)
            return

        if key == "micro_processing_ab_shortcut":
            requested = str(value or "").strip()
            if requested and shortcut_trigger_is_reserved(requested):
                self.backend_controller.status_changed.emit(
                    "That key is reserved for shortcut editing and cannot be mapped"
                )
                self._sync_tune_shortcut_runtime_ui()
                return
            sequence = QKeySequence(requested)
            trigger = normalize_shortcut_trigger(
                sequence.toString(QKeySequence.PortableText)
            )
            backend = self.shortcut_manager.backend_state("micro.tune.toggle")
            bridge = getattr(self, "_global_tune_shortcut", None)

            # KDE/KGlobalAccel path: the editor in K-Sounds is authoritative
            # for user edits and writes straight into KDE. SYNC from the helper
            # then updates the registry/settings, so a rejected conflict cleanly
            # snaps the editor back to the existing system binding.
            setter = getattr(bridge, "set_trigger", None)
            if backend.authoritative and callable(setter):
                if not setter(trigger):
                    self.backend_controller.status_changed.emit(
                        "Could not update the global shortcut"
                    )
                    self._sync_tune_shortcut_runtime_ui()
                else:
                    self.backend_controller.status_changed.emit(
                        f"Updating global TUNE shortcut — {trigger or 'cleared'}"
                    )
                return

            if backend.authoritative:
                # XDG portal fallback cannot silently remap. Keep its system
                # binding authoritative rather than pretending the edit worked.
                self._sync_tune_shortcut_runtime_ui()
                return

            self.shortcut_registry.set_binding(
                "micro.tune.toggle", trigger=trigger
            )
            self._refresh_micro_processing_ab_shortcut(restart_global=True)
            self._save_shortcut_registry_later()
            return

        if key == "background_enabled":
            settings.wallpaper_enabled = bool(value)
            self._background_source = self._load_background_source_from_settings()
            self._background_scaled_cache = QPixmap()
            self._refresh_background()
            self._save_visual_settings_later()
            self._sync_shared_visual_settings(key)
            return

        if key == "background_path":
            settings.wallpaper_path = str(value or "")
            settings.wallpaper_enabled = bool(settings.wallpaper_path)
            self._background_source = self._load_background_source_from_settings()
            self._background_scaled_cache = QPixmap()
            self._refresh_background()
            self._save_visual_settings_later()
            self._sync_shared_visual_settings(key)
            return

        try:
            numeric = max(0, min(100, int(value)))
        except Exception:
            return

        if key == "blur":
            settings.glass_background_blur = numeric
            self.background_blur.setBlurRadius(float(numeric))
            self._save_visual_settings_later()
            self._sync_shared_visual_settings(key)
            return

        if key == "saturation":
            settings.glass_background_saturation = numeric
            self._background_saturation = self._background_saturation_factor_from_slider(numeric)
            self._save_visual_settings_later()
            self._refresh_background()
            self._sync_shared_visual_settings(key)
            return

        if key == "contrast":
            settings.glass_background_contrast = numeric
            self._background_contrast = self._background_contrast_factor_from_slider(numeric)
            self._save_visual_settings_later()
            self._refresh_background()
            self._sync_shared_visual_settings(key)
            return

        if key == "darkness":
            settings.glass_background_darkness = numeric
            self._background_darkness = int(40 + numeric * 2.0)
            self._request_visual_style_update()
            self._save_visual_settings_later()
            self._sync_shared_visual_settings(key)
            return

        if key == "glass":
            settings.glass_opacity = numeric
            self._glass_opacity = int(70 + numeric * 1.55)
            self._request_visual_style_update()
            self._save_visual_settings_later()
            self._sync_shared_visual_settings(key)
            return


    def _request_visual_style_update(self) -> None:
        timer = getattr(self, "_visual_style_timer", None)
        if timer is not None:
            timer.start()
        else:
            self._apply_visual_style()

    def _apply_visual_style(self) -> None:
        glass = max(0, min(255, self._glass_opacity))
        hover = max(0, min(255, glass + 18))
        dim = max(0, min(255, self._background_darkness))

        wash = 0
        dynamic_style = f"""
QFrame#backgroundWash {{
    background: rgba(90, 130, 255, {wash});
    border: none;
}}

QFrame#backgroundTint {{
    background: rgba(0, 0, 0, {dim});
    border: none;
}}

QFrame#navRail,
QFrame#channelCard,
QFrame#drawer {{
    background: rgba(0, 0, 0, {glass});
    border: none;
    border-radius: 15px;
}}

QFrame#channelCard:hover {{
    background: rgba(5, 13, 22, {hover});
    border: none;
}}

QFrame#channelCard[voiceFxActive="true"] {{
    background: rgba(108, 78, 14, {max(150, glass)});
    border: 1px solid rgba(255, 207, 66, 100);
}}

QFrame#channelCard[voiceFxActive="true"]:hover {{
    background: rgba(132, 95, 18, {max(166, hover)});
    border: 1px solid rgba(255, 222, 96, 128);
}}

QFrame#channelCard[muted="true"] {{
    background: rgba(82, 18, 24, {max(175, glass)});
    border: none;
}}

QFrame#channelCard[muted="true"]:hover {{
    background: rgba(104, 22, 31, {max(190, hover)});
    border: none;
}}
"""
        app = QApplication.instance()
        if app is not None and app.styleSheet() != STYLE:
            # Keep the static stylesheet application-wide, but avoid rebuilding
            # the whole app stylesheet for every visual slider tick.
            app.setStyleSheet(STYLE)

        # Dynamic glass/background colors only need to affect this window tree.
        self.setStyleSheet(dynamic_style)
        if getattr(self, "_ksh_flat_foreground", None) is not None:
            self._ksh_flat_foreground.invalidate_background()

    def _sync_channel_cards_from_settings(self, force: bool = False) -> None:
        # Legacy fallback only; intentionally not scheduled.
        # Glass should update from its own frontend/controller state.
        try:
            mtime_ns = SETTINGS_PATH.stat().st_mtime_ns
        except Exception:
            return

        if not force and mtime_ns == self._settings_sync_mtime_ns:
            return

        self._settings_sync_mtime_ns = mtime_ns
        states = _read_saved_mixer_channel_state()
        if not states:
            return

        for card in self.channel_cards:
            key = str(getattr(card, "channel_key", "") or "").strip()
            if not key or key not in states:
                continue
            volume, muted = states[key]
            card.sync_from_saved_state(volume=volume, muted=muted)

    def _start_overlay_renderer_if_enabled(self) -> None:
        """Start the existing desktop HUD renderer when Glass starts.

        This does not render a new overlay in Glass. It only launches the
        already-existing HUD helper, and only when the Overlay setting is on.
        The Overlay checkbox still controls whether Glass emits HUD messages.
        """
        settings = getattr(self.backend_controller, "settings", None)
        if not bool(getattr(settings, "overlay_enabled", False)):
            return

        launcher_override = os.environ.get("KSH_HUD_LAUNCHER", "").strip()
        launcher = Path(launcher_override).expanduser() if launcher_override else HUD_LAUNCHER
        if not launcher.exists() or not os.access(launcher, os.X_OK):
            return

        try:
            subprocess.Popen(
                [str(launcher), "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            return

    def _stop_overlay_renderer(self) -> None:
        timer = getattr(self, "_hud_watchdog_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

        launcher_override = os.environ.get("KSH_HUD_LAUNCHER", "").strip()
        launcher = Path(launcher_override).expanduser() if launcher_override else HUD_LAUNCHER
        if not launcher.exists() or not os.access(launcher, os.X_OK):
            return

        try:
            subprocess.Popen(
                [str(launcher), "stop"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            return

    def _sync_overlay_watchdog(self) -> None:
        enabled = bool(getattr(self.backend_controller.settings, "overlay_enabled", False))
        timer = getattr(self, "_hud_watchdog_timer", None)
        if enabled:
            self._start_overlay_renderer_if_enabled()
            if (
                timer is not None
                and not timer.isActive()
                and os.environ.get("KSH_HUD_WATCHDOG_DISABLED", "").strip() != "1"
            ):
                timer.start()
            return

        self._stop_overlay_renderer()

    def _show_overlay_message(self, text: str, muted_active: bool = False) -> None:
        self.overlay.set_enabled(bool(getattr(self.backend_controller.settings, "overlay_enabled", False)))
        self.overlay.show_message(str(text or ""), muted_active=bool(muted_active))

    def _device_watch_process_is_marked(self, pid: int) -> bool:
        if pid <= 0:
            return False

        proc_dir = Path("/proc") / str(pid)

        try:
            cmdline = proc_dir.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            return False

        if "pactl" not in cmdline or "subscribe" not in cmdline:
            return False

        try:
            environ = proc_dir.joinpath("environ").read_bytes()
        except Exception:
            return False

        return b"KSH_RUNTIME_ROLE=glass_device_watch" in environ

    def _cleanup_stale_device_watch_processes(self) -> None:
        current_pid = 0
        process = getattr(self, "_device_watch_process", None)
        if process is not None:
            try:
                current_pid = int(process.processId())
            except Exception:
                current_pid = 0

        proc_root = Path("/proc")
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue

            pid = int(proc_dir.name)
            if pid in {os.getpid(), current_pid}:
                continue

            if not self._device_watch_process_is_marked(pid):
                continue

            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                continue
            except Exception:
                continue

            time.sleep(0.04)

            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except Exception:
                continue

            try:
                os.kill(pid, 9)
            except Exception:
                pass

    def _stop_device_watch_process(self) -> None:
        self._device_watch_shutting_down = True
        process = getattr(self, "_device_watch_process", None)
        if process is None:
            return

        try:
            if process.state() == QProcess.ProcessState.NotRunning:
                return

            process.terminate()
            if not process.waitForFinished(700):
                process.kill()
                process.waitForFinished(700)
        except Exception:
            pass

    def _refresh_app_routes_after_stream_event(self) -> None:
        controller = getattr(self, "backend_controller", None)
        if controller is None:
            return

        refresh = getattr(controller, "_reapply_saved_app_routes", None)
        if not callable(refresh):
            return

        try:
            refresh()
        except Exception as exc:
            controller.status_changed.emit(f"Apps auto-sort error — {exc}")

    def _start_device_watch_process(self) -> None:
        process = getattr(self, "_device_watch_process", None)
        if process is None:
            return

        if os.environ.get("KSH_DEVICE_WATCHER_DISABLED", "").strip() == "1":
            return

        try:
            if process.state() != QProcess.ProcessState.NotRunning:
                return

            self._cleanup_stale_device_watch_processes()

            env = QProcessEnvironment.systemEnvironment()
            env.insert("KSH_RUNTIME_ROLE", "glass_device_watch")
            env.insert("KSH_GLASS_PARENT_PID", str(os.getpid()))
            process.setProcessEnvironment(env)

            self._device_watch_shutting_down = False
            process.start()
        except Exception as exc:
            self.backend_controller.status_changed.emit(f"Audio device watcher unavailable — {exc}")

    def _handle_device_watch_finished(self, *_args) -> None:
        # pactl subscribe should normally stay alive. If Pulse/PipeWire restarts,
        # retry without falling back to periodic polling.
        if getattr(self, "_device_watch_shutting_down", False):
            return
        if os.environ.get("KSH_DEVICE_WATCHER_DISABLED", "").strip() == "1":
            return
        QTimer.singleShot(2000, self._start_device_watch_process)

    def _handle_device_watch_events(self) -> None:
        process = getattr(self, "_device_watch_process", None)
        if process is None:
            return

        try:
            data = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        except Exception:
            return

        interesting = False
        app_route_interesting = False

        for line in data.lower().splitlines():
            # Device selectors only need topology events. App routing also watches
            # sink-input stream events, but with a separate lightweight debounce.
            if " on sink-input #" in line:
                app_route_interesting = True
                continue

            if (
                " on sink #" in line
                or " on source #" in line
                or " on card #" in line
                or " on server" in line
            ):
                interesting = True

        if app_route_interesting:
            self._app_route_watch_debounce.start()

        if interesting:
            self._device_watch_debounce.start()


    def _device_watch_current_signature(self) -> tuple:
        output_pairs = tuple(self.backend_controller.available_output_targets())
        input_pairs = tuple(self.backend_controller.available_input_targets())
        channel_targets = tuple(
            (str(channel[4]), self.backend_controller.channel_primary_target(str(channel[4])))
            for channel in CHANNELS
        )
        return output_pairs, input_pairs, channel_targets

    def _refresh_device_selectors_if_needed(self, force: bool = False) -> None:
        if self.backend_controller is None or not getattr(self, "channel_cards", None):
            return

        try:
            signature = self._device_watch_current_signature()
        except Exception as exc:
            self.backend_controller.status_changed.emit(f"Device watcher error — {exc}")
            return

        if not force and signature == self._device_watch_signature:
            return

        first_run = not bool(self._device_watch_signature)
        self._device_watch_signature = signature

        cards_by_key = {
            str(getattr(card, "channel_key", "") or "").strip(): card
            for card in self.channel_cards
        }

        for channel in CHANNELS:
            _name, _icon, fallback_devices, _fallback_value, channel_key = channel
            key = str(channel_key or "").strip()
            card = cards_by_key.get(key)
            if card is None:
                continue
            choices = self._channel_device_choices(key, fallback_devices)
            current = self._channel_device_label(key, choices)
            card.sync_device_choices(choices, current)

        if not first_run:
            self.backend_controller.status_changed.emit("Audio devices updated")

    def _channel_device_pairs(self, channel_key: str) -> list[tuple[str, str]]:
        key = str(channel_key or "").strip()
        if key == "micro":
            return self.backend_controller.available_input_targets()
        return self.backend_controller.available_output_targets()

    def _channel_device_choices(self, channel_key: str, fallback_devices: list[str]) -> list[str]:
        key = str(channel_key or "").strip()
        pairs = self._channel_device_pairs(key)
        labels = [label for label, _name in pairs if str(label or "").strip()]

        target = self.backend_controller.channel_primary_target(key)
        if target and not any(name == target for _label, name in pairs):
            label = self.backend_controller.label_for_target(target, input_device=(key == "micro")) or target
            missing_label = _format_missing_device_label(label)
            if missing_label not in labels:
                labels.insert(0, missing_label)

        return labels or list(fallback_devices or [])

    def _channel_device_label(self, channel_key: str, fallback_devices: list[str]) -> str:
        key = str(channel_key or "").strip()
        target = self.backend_controller.channel_primary_target(key)

        if target:
            pairs = self._channel_device_pairs(key)
            label = self.backend_controller.label_for_target(target, input_device=(key == "micro")) or target
            if any(name == target for _label, name in pairs):
                return label
            return _format_missing_device_label(label)

        return fallback_devices[0] if fallback_devices else ""

    def _channel_device_target_from_label(self, channel_key: str, label: str) -> str:
        key = str(channel_key or "").strip()
        name = str(label or "").strip()
        if not name or _is_missing_device_label(name):
            return ""
        if key == "micro":
            return self.backend_controller.resolve_input_label(name)
        return self.backend_controller.resolve_output_label(name)

    def _channel_device_is_favorite(self, channel_key: str, label: str) -> bool:
        key = str(channel_key or "").strip()
        target = self._channel_device_target_from_label(key, label)
        if not target:
            return False
        return self.backend_controller.is_favorite_device_target(target, input_device=(key == "micro"))

    def _set_channel_device_favorite(self, channel_key: str, label: str, favorite: bool) -> bool:
        key = str(channel_key or "").strip()
        target = self._channel_device_target_from_label(key, label)
        if not target:
            return False
        ok = self.backend_controller.set_favorite_device_target(
            target, bool(favorite), input_device=(key == "micro")
        )
        return bool(ok)

    def _cycle_channel_favorite_device(self, channel_key: str) -> bool:
        key = str(channel_key or "").strip()
        input_device = key == "micro"
        available_pairs = self._channel_device_pairs(key)
        available_names = {name for _label, name in available_pairs}
        favorites = [
            target
            for target in self.backend_controller.favorite_device_targets(input_device=input_device)
            if target in available_names
        ]
        if not favorites:
            self.backend_controller.status_changed.emit(
                f"{CHANNEL_SHORTCUT_LABELS.get(key, key.upper())}: no favorite device available"
            )
            return False
        current = self.backend_controller.channel_primary_target(key)
        if current in favorites:
            next_target = favorites[(favorites.index(current) + 1) % len(favorites)]
        else:
            next_target = favorites[0]
        if not self.backend_controller.set_channel_primary_target(key, next_target):
            return False
        self._refresh_device_selectors_if_needed(force=True)
        return True

    def _set_channel_device(self, channel_key: str, label: str) -> None:
        key = str(channel_key or "").strip()
        name = str(label or "").strip()

        if _is_missing_device_label(name):
            self.backend_controller.status_changed.emit(f"Device not found: {_plain_missing_device_label(name)}")
            return

        target = self._channel_device_target_from_label(key, name)

        if not target:
            self.backend_controller.status_changed.emit(f"Unsupported device route: {key} → {name}")
            return

        self.backend_controller.set_channel_primary_target(key, target)

    def _channel_state_for_card(self, channel_key: str, fallback_value: int) -> tuple[int, bool]:
        state = self.backend_controller.channel_state(channel_key)
        if state is None:
            return max(0, min(100, int(fallback_value))), False
        return state

    def _channel_stereo_width(self, channel_key: str) -> int:
        return self.backend_controller.channel_stereo_width(channel_key)

    def _sync_channel_card_from_backend(self, channel_key: str, volume: int, muted: bool) -> None:
        key = str(channel_key or "").strip()
        for card in self.channel_cards:
            if str(getattr(card, "channel_key", "") or "").strip() == key:
                card.sync_from_saved_state(volume=volume, muted=muted, animate_volume=True)
                return

    def _send_channel_volume(self, channel_key: str, value: int) -> bool:
        return self.backend_controller.set_channel_volume(
            channel_key,
            max(0, min(100, int(value))),
        )

    def _send_channel_volume_live(self, channel_key: str, value: int) -> bool:
        return self.backend_controller.set_channel_volume_live(
            channel_key,
            max(0, min(100, int(value))),
        )

    def _open_micro_eq_editor(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("MICRO EQ")
        dialog.setModal(True)
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        title = QLabel("MICRO EQ · dedicated microphone profiles")
        title.setObjectName("microProcessingTitle")
        layout.addWidget(title)
        hint = QLabel("MICRO EQ profiles are independent from ALL / GAME / CHAT / MEDIA / MORE output EQ presets.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        panel = EqPanel(
            self.backend_controller,
            channels=[("MICRO", "micro")],
            show_channel_selector=False,
            preset_shortcut_state_getter=self._micro_eq_preset_shortcut_state,
            preset_shortcut_change_callback=self._update_micro_eq_preset_shortcut,
        )
        layout.addWidget(panel, 1)
        dialog.exec()

    def _open_micro_processing_dialog(self) -> None:
        # Historical method name kept for shortcut/tests compatibility. TUNE is
        # now a first-class Glass page rather than a modal dialog.
        self._show_drawer_nav_page(3)

    def _sync_micro_voice_fx_card_state(self, active: bool) -> None:
        for card in getattr(self, "channel_cards", []):
            if str(getattr(card, "channel_key", "") or "") == "micro":
                card.set_voice_fx_active(bool(active))
                break

    def _open_micro_voice_fx_dialog(self) -> None:
        # Historical method name kept for compatibility. VFX is now a normal
        # Glass page and shares the same central state as shortcuts/Remote.
        self._show_drawer_nav_page(4)

    def _set_micro_hard_gate_enabled(self, enabled: bool) -> bool:
        return self.backend_controller.set_micro_hard_gate_enabled(bool(enabled))

    def _set_micro_hard_gate_threshold(self, value: int) -> bool:
        return self.backend_controller.set_micro_hard_gate_threshold(max(0, min(100, int(value))))

    def _set_channel_muted(self, channel_key: str, muted: bool) -> bool:
        return self.backend_controller.set_channel_muted(channel_key, bool(muted))

    def _set_channel_stereo_width(self, channel_key: str, value: int) -> bool:
        return self.backend_controller.set_channel_stereo_width(channel_key, int(value))

    def _start_meter_updates(self) -> None:
        try:
            interval_ms = int(os.environ.get("KSH_METER_INTERVAL_MS", "25") or "25")
        except Exception:
            interval_ms = 33
        interval_ms = max(20, min(100, interval_ms))

        self.meter_timer = QTimer(self)
        self.meter_timer.setInterval(interval_ms)
        self.meter_timer.timeout.connect(self._animate_meters)
        self._set_meters_visible(bool(getattr(self.backend_controller.settings, "visualizer_enabled", True)))


    def _animate_meters(self) -> None:
        if (
            getattr(self, "_window_runtime_shutdown", False)
            or not bool(getattr(self.backend_controller.settings, "visualizer_enabled", True))
        ):
            return

        cards = tuple(self.channel_cards)
        keys = tuple(
            str(getattr(card, "channel_key", "") or "").strip()
            for card in cards
        )
        levels = self.backend_controller.meter_levels_batch(
            key for key in keys if key
        )

        for card, key in zip(cards, keys):
            left, right = levels.get(key, (0.0, 0.0)) if key else (0.0, 0.0)
            card.set_meter_levels(left, right)
            card.tick_meters()

    def _save_geometry_now(self) -> None:
        _save_window_geometry(self, self._geometry_settings_key)

    def _restore_geometry_after_show(self) -> None:
        _restore_window_geometry(self, self._geometry_settings_key, 1320, 560, 860, 430)

    def _queue_geometry_save(self) -> None:
        _queue_window_geometry_save(self, self._geometry_settings_key, self._geometry_save_timer)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._restore_geometry_after_show)
        QTimer.singleShot(350, self._restore_geometry_after_show)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._queue_geometry_save()

    def resizeEvent(self, event) -> None:
        # Do not rescale/reprocess the background during live resize.
        # QLabel scaledContents keeps the preview responsive.
        super().resizeEvent(event)
        self._queue_geometry_save()

    def _queue_background_refresh(self) -> None:
        self._background_refresh_timer.start()



    def _background_saturation_factor_from_slider(self, value: int) -> float:
        """Map UI saturation slider to real image saturation.

        0   -> 0.0 grayscale
        50  -> 1.0 original image colors
        100 -> 2.0 boosted colors
        """
        return _background_factor_from_slider(value)


    def _background_contrast_factor_from_slider(self, value: int) -> float:
        """Map UI contrast slider to real image contrast.

        0   -> flat/low contrast
        50  -> original contrast
        100 -> boosted contrast
        """
        return _background_factor_from_slider(value)

    def _saturate_background_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Apply saturation and contrast to the cached/scaled background pixmap.

        Slider mapping:
        0   -> reduced
        50  -> normal
        100 -> boosted
        """
        return _adjust_background_pixmap(
            pixmap,
            getattr(self, "_background_saturation", 1.0),
            getattr(self, "_background_contrast", 1.0),
        )



    def _saturate_pixmap(self, pixmap: QPixmap) -> QPixmap:
        # V10: no pixel-by-pixel saturation processing.
        # The Settings saturation slider applies real saturation to the scaled background pixmap.
        return pixmap

    def _refresh_background(self) -> None:
        self._queue_background_refresh()

    def _refresh_background_now(self) -> None:
        if self._background_source.isNull():
            self.background_label.clear()
            if getattr(self, "_ksh_flat_foreground", None) is not None:
                self._ksh_flat_foreground.invalidate_background()
            return



        # Keep the normal background reasonably detailed, but process a smaller
        # pixmap when saturation is not neutral. Pixel saturation is UI-only and
        # must stay responsive while the slider is being dragged.
        try:
            saturation = float(getattr(self, "_background_saturation", 1.0))
        except Exception:
            saturation = 1.0
        try:
            contrast = float(getattr(self, "_background_contrast", 1.0))
        except Exception:
            contrast = 1.0

        neutral = abs(saturation - 1.0) < 0.01 and abs(contrast - 1.0) < 0.01
        scaled = self._background_scaled_cache
        if scaled.isNull():
            scaled = self._background_source.scaled(
                QSize(1280, 720),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self._background_scaled_cache = scaled

        self.background_label.setPixmap(self._saturate_background_pixmap(scaled))
        if getattr(self, "_ksh_flat_foreground", None) is not None:
            self._ksh_flat_foreground.invalidate_background()

    def _edges_for_global_pos(self, global_pos: QPoint) -> set[str]:
        local = self.mapFromGlobal(global_pos)
        margin = self.resize_margin
        rect = self.rect()
        edges: set[str] = set()

        if local.x() <= margin:
            edges.add("left")
        elif local.x() >= rect.width() - margin:
            edges.add("right")

        if local.y() <= margin:
            edges.add("top")
        elif local.y() >= rect.height() - margin:
            edges.add("bottom")

        return edges

    def _cursor_for_edges(self, edges: set[str]) -> Qt.CursorShape:
        if {"left", "top"}.issubset(edges) or {"right", "bottom"}.issubset(edges):
            return Qt.SizeFDiagCursor
        if {"right", "top"}.issubset(edges) or {"left", "bottom"}.issubset(edges):
            return Qt.SizeBDiagCursor
        if "left" in edges or "right" in edges:
            return Qt.SizeHorCursor
        if "top" in edges or "bottom" in edges:
            return Qt.SizeVerCursor
        return Qt.ArrowCursor


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("K-Sounds Hub")
    app.setApplicationDisplayName("K-Sounds Hub")
    app.setOrganizationName("K-Sounds")
    app.setDesktopFileName("ksounds-hub")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)
    window = PreviewWindow()
    app.aboutToQuit.connect(window._save_all_window_geometry_now)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
