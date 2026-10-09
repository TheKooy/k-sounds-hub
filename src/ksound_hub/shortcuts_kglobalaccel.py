from __future__ import annotations

from pathlib import Path
import os
from typing import Callable

from .shortcuts_portal_bind import GlobalShortcutState


def helper_candidates() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    app_root = here.parents[2]
    override = os.environ.get("KSH_KGLOBALACCEL_HELPER", "").strip()

    values: list[Path] = []
    if override:
        values.append(Path(override).expanduser())
    values.extend(
        [
            app_root / "bin/ksound-kglobalaccel-helper",
            app_root / "tools/kglobalaccel_helper/build/ksound-kglobalaccel-helper",
        ]
    )
    return tuple(values)


def find_helper() -> Path | None:
    for candidate in helper_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


class _KdeShortcutHub:
    """One native KGlobalAccel helper process shared by all K-Sounds actions."""

    def __init__(self) -> None:
        self._process = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._ready = False
        self._clients: dict[str, KdeGlobalShortcut] = {}

    @property
    def ready(self) -> bool:
        return bool(self._ready and self._process is not None)

    def _write(self, text: str) -> bool:
        process = self._process
        if process is None:
            return False
        try:
            from PySide6.QtCore import QProcess
            if process.state() == QProcess.NotRunning:
                return False
            payload = (text.rstrip("\n") + "\n").encode("utf-8")
            return process.write(payload) == len(payload)
        except Exception:
            return False

    @staticmethod
    def _clean_field(value: str) -> str:
        return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()

    def _register_client(self, client: "KdeGlobalShortcut") -> bool:
        return self._write(
            "REGISTER\t{}\t{}\t{}".format(
                self._clean_field(client.shortcut_id),
                self._clean_field(client.description),
                self._clean_field(client.trigger),
            )
        )

    def ensure_started(self) -> bool:
        if self.ready:
            return True
        if self._process is not None:
            try:
                from PySide6.QtCore import QProcess
                if self._process.state() != QProcess.NotRunning:
                    return True
            except Exception:
                pass
        helper = find_helper()
        if helper is None:
            for client in tuple(self._clients.values()):
                client._hub_error("KGlobalAccel helper not found")
            return False

        try:
            from PySide6.QtCore import QProcess
        except Exception as exc:
            for client in tuple(self._clients.values()):
                client._hub_error(f"Qt process support unavailable: {exc}")
            return False

        process = QProcess()
        process.setProgram(str(helper))
        process.setArguments(["--daemon", "--app-id", "k-sounds-hub"])
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._process_finished)

        self._process = process
        self._ready = False
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        process.start()
        if not process.waitForStarted(3000):
            detail = process.errorString() or "could not start KGlobalAccel helper"
            for client in tuple(self._clients.values()):
                client._hub_error(detail)
            self.stop()
            return False

        # A started process is not enough: daemon mode must also establish its
        # stdin notifier and report READY. Catch a silent half-start instead of
        # leaving every shortcut looking configured but inert.
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1800, self._startup_watchdog)
        except Exception:
            pass
        return True

    def _startup_watchdog(self) -> None:
        process = self._process
        if process is None or self._ready:
            return
        try:
            from PySide6.QtCore import QProcess
            if process.state() == QProcess.NotRunning:
                return
        except Exception:
            pass
        detail = self._stderr_buffer.strip() or "KGlobalAccel helper started but never became ready"
        for client in tuple(self._clients.values()):
            client._hub_error(detail)
        self.stop()

    def attach(self, client: "KdeGlobalShortcut") -> bool:
        self._clients[client.shortcut_id] = client
        if not self.ensure_started():
            return False
        if self._ready:
            return self._register_client(client)
        return True

    def detach(self, client: "KdeGlobalShortcut") -> None:
        current = self._clients.get(client.shortcut_id)
        if current is client:
            self._clients.pop(client.shortcut_id, None)
        if not self._clients:
            self.stop()

    def set_trigger(self, shortcut_id: str, trigger: str) -> bool:
        return self._write(
            f"SET\t{self._clean_field(shortcut_id)}\t{self._clean_field(trigger)}"
        )

    def refresh(self, shortcut_id: str) -> bool:
        return self._write(f"REFRESH\t{self._clean_field(shortcut_id)}")

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        raw = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not raw:
            return
        self._stdout_buffer += raw
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._handle_line(line.rstrip("\r"))

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        raw = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
        if raw:
            self._stderr_buffer = (self._stderr_buffer + raw)[-4000:]

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        parts = line.split("\t")
        event = parts[0].strip().upper()

        if event == "PONG":
            return

        # Daemon-level errors have no shortcut id. Fan them out instead of
        # accidentally treating the message text as an id and swallowing it.
        if event == "ERROR" and (len(parts) < 3 or parts[1].strip() not in self._clients):
            detail = "\t".join(parts[1:]).strip() or "KGlobalAccel helper error"
            for client in tuple(self._clients.values()):
                client._hub_error(detail)
            return

        # Daemon-level readiness. Register every action only once the helper is
        # live so 20+ mixer bindings still use a single native process.
        if event == "READY" and len(parts) >= 2 and parts[1].strip().upper() == "KGLOBALACCEL":
            self._ready = True
            for client in tuple(self._clients.values()):
                self._register_client(client)
            return

        shortcut_id = parts[1].strip() if len(parts) > 1 else ""
        client = self._clients.get(shortcut_id)
        if client is not None:
            client._handle_hub_event(event, parts)

    def _process_finished(self, *_args) -> None:
        self._read_stdout()
        self._read_stderr()
        detail = self._stderr_buffer.strip() or "KGlobalAccel helper exited"
        self._ready = False
        self._process = None
        for client in tuple(self._clients.values()):
            client._hub_error(detail)

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._ready = False
        if process is None:
            return
        try:
            process.readyReadStandardOutput.disconnect(self._read_stdout)
        except Exception:
            pass
        try:
            process.readyReadStandardError.disconnect(self._read_stderr)
        except Exception:
            pass
        try:
            process.finished.disconnect(self._process_finished)
        except Exception:
            pass
        try:
            from PySide6.QtCore import QProcess
            if process.state() != QProcess.NotRunning:
                process.terminate()
                if not process.waitForFinished(1200):
                    process.kill()
                    process.waitForFinished(1200)
        except Exception:
            pass


_SHARED_HUB = _KdeShortcutHub()


class KdeGlobalShortcut:
    """One logical shortcut backed by the shared KDE KGlobalAccel helper."""

    def __init__(
        self,
        *,
        shortcut_id: str,
        description: str,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None] | None = None,
        on_state_change: Callable[[GlobalShortcutState], None] | None = None,
    ) -> None:
        self.shortcut_id = str(shortcut_id or "").strip()
        self.description = str(description or "").strip()
        if not self.shortcut_id or not self.description:
            raise ValueError("shortcut_id and description must not be empty")

        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._on_state_change = on_state_change
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self.trigger = ""
        self.detail = "not started"
        self._sticky_error = ""
        self._attached = False

    def state(self) -> GlobalShortcutState:
        return GlobalShortcutState(
            active=self.active,
            trigger=self.trigger,
            detail=self.detail,
            portal_ready=self.portal_ready,
            registered=self.registered,
            kde_enabled=self.kde_enabled,
            kde_trigger=self.kde_trigger,
            backend="kglobalaccel",
            inline_remap=True,
        )

    def _emit_state(self) -> None:
        if self._on_state_change is not None:
            self._on_state_change(self.state())

    def _hub_error(self, message: str) -> None:
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self.detail = str(message or "KGlobalAccel helper unavailable")
        self._emit_state()

    def _handle_hub_event(self, event: str, parts: list[str]) -> None:
        if event in {"READY", "REGISTERED"}:
            # REGISTER is immediately followed by SYNC.  Do not publish an
            # intermediate "registered but disabled" state here: Glass would
            # otherwise clear a just-entered binding before KDE reports the
            # authoritative trigger on the next line.
            self.portal_ready = True
            self.registered = True
            self.detail = "KDE KGlobalAccel ready; waiting for sync"
            return

        if event == "DEFAULT_CONFLICT":
            conflict = "\t".join(parts[2:]).strip()
            self._sticky_error = (
                f"default shortcut already used — {conflict}"
                if conflict
                else "default shortcut already used"
            )
            self.detail = self._sticky_error
            self._emit_state()
            return

        if event == "SYNC":
            state = parts[2].strip().upper() if len(parts) > 2 else "DISABLED"
            trigger = "\t".join(parts[3:]).strip() if len(parts) > 3 else ""
            self.portal_ready = True
            self.registered = True
            self.kde_enabled = state == "ENABLED"
            self.kde_trigger = trigger if self.kde_enabled else ""
            self.active = self.kde_enabled
            if self.kde_enabled:
                self.trigger = trigger or self.trigger
                normal_detail = f"KDE global shortcut active — {trigger}" if trigger else "KDE global shortcut active"
            else:
                normal_detail = "KDE global shortcut disabled"
            self.detail = self._sticky_error or normal_detail
            self._emit_state()
            return

        if event == "ACTIVATED":
            if self.portal_ready and self.kde_enabled:
                self._on_activate()
            return

        if event == "DEACTIVATED":
            if self.portal_ready and self.kde_enabled and self._on_deactivate is not None:
                self._on_deactivate()
            return

        if event == "SET_OK":
            # SET_OK is also immediately followed by SYNC.  SYNC is the single
            # source of truth for trigger/enabled state; emitting here would
            # momentarily expose the previous state and can undo an in-app remap.
            self._sticky_error = ""
            self.detail = "shortcut updated in KDE; waiting for sync"
            return

        if event == "CONFLICT":
            trigger = parts[2].strip() if len(parts) > 2 else ""
            owner = "\t".join(parts[3:]).strip() if len(parts) > 3 else ""
            if trigger and owner:
                self._sticky_error = f"{trigger} is already used by {owner}"
            elif trigger:
                self._sticky_error = f"{trigger} is already used by another global shortcut"
            else:
                self._sticky_error = "shortcut already used by another global shortcut"
            self.detail = self._sticky_error
            self._emit_state()
            return

        if event in {"SET_ERROR", "COMMAND_ERROR", "ERROR"}:
            message = "\t".join(parts[2:]).strip() if len(parts) > 2 else ""
            self.detail = message or "KGlobalAccel helper error"
            if event == "ERROR":
                self.portal_ready = False
                self.active = False
            self._emit_state()

    def start(self, trigger: str) -> GlobalShortcutState:
        self.trigger = str(trigger or "").strip()
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self.detail = "starting KDE helper"
        self._sticky_error = ""
        self._attached = _SHARED_HUB.attach(self)
        if not self._attached:
            self._hub_error(self.detail if self.detail != "starting KDE helper" else "could not start KGlobalAccel helper")
        return self.state()

    def set_trigger(self, trigger: str) -> bool:
        self.trigger = str(trigger or "").strip()
        return bool(self._attached and _SHARED_HUB.set_trigger(self.shortcut_id, self.trigger))

    def refresh(self) -> bool:
        return bool(self._attached and _SHARED_HUB.refresh(self.shortcut_id))

    def stop(self) -> None:
        if self._attached:
            _SHARED_HUB.detach(self)
        self._attached = False
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""


def stop_shared_helper() -> None:
    _SHARED_HUB.stop()
