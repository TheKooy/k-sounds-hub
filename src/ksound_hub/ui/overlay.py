from __future__ import annotations

import json
import threading
import time

from PySide6.QtCore import QObject

from ..config import HUD_SHARED_STATE_DIR, HUD_SHARED_STATE_PATH


class OverlayManager(QObject):
    """
    Non-blocking bridge to the native HUD overlay.

    UI callers only publish the newest desired HUD payload. A dedicated daemon
    thread performs the filesystem write/replace, so rapid volume updates can
    never block the Qt GUI thread. Pending volume/HUD updates are coalesced:
    when several arrive while the writer is busy, only the newest one is kept.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = False
        self._seq = 0
        self._hud_state_dir = HUD_SHARED_STATE_DIR
        self._hud_state_path = HUD_SHARED_STATE_PATH
        self._hud_state_dir.mkdir(parents=True, exist_ok=True)
        self._seq = self._load_last_sequence()

        self._writer_condition = threading.Condition()
        self._writer_pending: dict | None = None
        self._writer_stopping = False
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="ksounds-hud-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _load_last_sequence(self) -> int:
        if not self._hud_state_path.is_file():
            return 0
        try:
            data = json.loads(self._hud_state_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        try:
            return int(data.get("seq", 0))
        except Exception:
            return 0

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def show_message(self, text: str, duration_ms: int = 900, *, muted_active: bool = False) -> None:
        """Publish a HUD message without doing filesystem I/O on the GUI thread."""
        if not self._enabled or not text:
            return

        self._seq += 1
        payload = {
            "seq": self._seq,
            "text": str(text),
            "visible": True,
            "durationMs": int(duration_ms),
            "mutedActive": bool(muted_active),
            "timestamp": int(time.time() * 1000),
        }

        with self._writer_condition:
            # Latest wins. The HUD is a live display, not an event history.
            self._writer_pending = payload
            self._writer_condition.notify()

    def _writer_loop(self) -> None:
        while True:
            with self._writer_condition:
                while self._writer_pending is None and not self._writer_stopping:
                    self._writer_condition.wait()
                if self._writer_stopping:
                    return
                payload = self._writer_pending
                self._writer_pending = None

            if payload is None:
                continue

            try:
                # Compact JSON and atomic replace keep the native QFileSystemWatcher
                # protocol intact while minimizing write/metadata overhead.
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"
                tmp_path = self._hud_state_path.with_suffix(".json.tmp")
                tmp_path.write_text(encoded, encoding="utf-8")
                tmp_path.replace(self._hud_state_path)
            except Exception:
                # HUD failure must never affect mixer/UI responsiveness.
                continue

    def shutdown(self) -> None:
        with self._writer_condition:
            self._writer_stopping = True
            self._writer_pending = None
            self._writer_condition.notify_all()

        thread = getattr(self, "_writer_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
