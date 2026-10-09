from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable

from .shortcuts_portal import PortalShortcutSpec, qt_sequence_to_xdg_trigger


def tune_shortcut_spec(trigger: str) -> PortalShortcutSpec:
    return PortalShortcutSpec(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        preferred_trigger=qt_sequence_to_xdg_trigger(trigger),
    )


def bind_shortcuts_options(handle_token: str) -> dict[str, str]:
    token = str(handle_token or "").strip()
    if not token:
        raise ValueError("handle_token must not be empty")
    return {"handle_token": token}


def bind_shortcuts_payload(specs) -> list[tuple[str, dict[str, str]]]:
    payload: list[tuple[str, dict[str, str]]] = []
    for spec in specs:
        shortcut_id = str(spec.shortcut_id or "").strip()
        description = str(spec.description or "").strip()
        trigger = str(spec.preferred_trigger or "").strip()
        if not shortcut_id or not description:
            raise ValueError("shortcut id and description must not be empty")

        values = {"description": description}
        if trigger:
            values["preferred_trigger"] = trigger
        payload.append((shortcut_id, values))

    if not payload:
        raise ValueError("at least one shortcut is required")
    return payload


@dataclass(slots=True, frozen=True)
class GlobalShortcutState:
    """Live state of the desktop-owned shortcut.

    ``portal_ready`` means the XDG/KDE backend is authoritative. ``registered``
    means KDE knows this action. ``kde_enabled`` means KDE currently has a key
    assigned. ``active`` is kept as the compatibility alias for that effective
    KDE-enabled state.
    """

    active: bool
    trigger: str = ""
    detail: str = ""
    portal_ready: bool = False
    registered: bool = False
    kde_enabled: bool = False
    kde_trigger: str = ""
    backend: str = "xdg-portal"
    inline_remap: bool = False


def helper_candidates() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    app_root = here.parents[2]
    override = os.environ.get("KSH_GLOBAL_SHORTCUTS_HELPER", "").strip()

    result: list[Path] = []
    if override:
        result.append(Path(override).expanduser())

    result.extend(
        [
            app_root / "bin/ksound-global-shortcuts-helper",
            app_root
            / "tools/global_shortcuts_helper/build/ksound-global-shortcuts-helper",
        ]
    )
    return tuple(result)


def find_helper() -> Path | None:
    for candidate in helper_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


class XdgGlobalShortcut:
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
        self._process = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._startup_loop = None
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self.trigger = ""
        self.detail = "not started"

    def state(self) -> GlobalShortcutState:
        return GlobalShortcutState(
            active=self.active,
            trigger=self.trigger,
            detail=self.detail,
            portal_ready=self.portal_ready,
            registered=self.registered,
            kde_enabled=self.kde_enabled,
            kde_trigger=self.kde_trigger,
        )

    def _emit_state(self) -> None:
        callback = self._on_state_change
        if callback is not None:
            callback(self.state())

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return

        raw = bytes(process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
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
        raw = bytes(process.readAllStandardError()).decode(
            "utf-8", errors="replace"
        )
        if raw:
            self._stderr_buffer = (self._stderr_buffer + raw)[-4000:]

    def _handle_line(self, line: str) -> None:
        if not line:
            return

        parts = line.split("\t")
        event = parts[0].strip().upper()

        if event == "PENDING":
            self.detail = "waiting for KDE Global Shortcuts confirmation"
            self._emit_state()
            return

        if event == "READY":
            self.portal_ready = True
            self.detail = "native XDG GlobalShortcuts session ready"
            # SYNC follows immediately and contains the authoritative KDE
            # enabled/disabled state. Do not publish a transient false OFF.
            loop = self._startup_loop
            if loop is not None and loop.isRunning():
                loop.quit()
            return

        if event == "SYNC":
            state = parts[2].strip().upper() if len(parts) > 2 else "MISSING"
            trigger = "\t".join(parts[3:]).strip() if len(parts) > 3 else ""
            self.portal_ready = True
            self.registered = state in {"ENABLED", "DISABLED"}
            self.kde_enabled = state == "ENABLED"
            self.kde_trigger = trigger if self.kde_enabled else ""
            self.active = self.kde_enabled
            if state == "ENABLED":
                self.detail = f"KDE shortcut active — {trigger}" if trigger else "KDE shortcut active"
            elif state == "DISABLED":
                self.detail = "KDE shortcut disabled"
            else:
                self.detail = "shortcut is not registered in KDE"
            self._emit_state()
            return

        if event == "ACTIVATED":
            if self.portal_ready and self.kde_enabled:
                self._on_activate()
            return

        if event == "DEACTIVATED":
            if (
                self.portal_ready
                and self.kde_enabled
                and self._on_deactivate is not None
            ):
                self._on_deactivate()
            return

        if event == "ERROR":
            self.active = False
            self.portal_ready = False
            self.registered = False
            self.kde_enabled = False
            self.kde_trigger = ""
            message = "\t".join(parts[1:]).strip()
            self.detail = message or "native helper reported an error"
            self._emit_state()
            loop = self._startup_loop
            if loop is not None and loop.isRunning():
                loop.quit()

    def _process_finished(self, *_args) -> None:
        self._read_stdout()
        self._read_stderr()

        if self.portal_ready:
            self.detail = "native global-shortcut helper exited"
        elif self.detail == "starting native XDG helper":
            extra = self._stderr_buffer.strip()
            self.detail = extra or "native global-shortcut helper exited"

        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self._emit_state()

        loop = self._startup_loop
        if loop is not None and loop.isRunning():
            loop.quit()

    def start(self, trigger: str) -> GlobalShortcutState:
        self.stop()

        raw_trigger = str(trigger or "").strip()
        self.trigger = raw_trigger

        helper = find_helper()
        if helper is None:
            self.detail = "native global-shortcut helper not found"
            self._emit_state()
            return self.state()

        try:
            from PySide6.QtCore import QProcess
        except Exception as exc:
            self.detail = f"Qt process support unavailable: {exc}"
            self._emit_state()
            return self.state()

        xdg_trigger = ""
        if raw_trigger:
            try:
                xdg_trigger = qt_sequence_to_xdg_trigger(raw_trigger)
            except Exception:
                xdg_trigger = ""

        args = [
            "--app-id",
            "k-sounds-hub",
            "--shortcut-id",
            self.shortcut_id,
            "--description",
            self.description,
        ]
        if xdg_trigger:
            args += ["--trigger", xdg_trigger]

        process = QProcess()
        process.setProgram(str(helper))
        process.setArguments(args)
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._process_finished)

        self._process = process
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        self.detail = "starting native XDG helper"
        self._stdout_buffer = ""
        self._stderr_buffer = ""

        process.start()
        if not process.waitForStarted(3000):
            self.detail = process.errorString() or "could not start native helper"
            self._emit_state()
            self.stop()
            return self.state()

        self.detail = "waiting for KDE shortcut state"
        self._emit_state()
        return self.state()

    def configure(self) -> bool:
        """Open KDE's configuration UI, or register the action if missing."""

        process = self._process
        if process is None:
            return False
        try:
            from PySide6.QtCore import QProcess
            if process.state() == QProcess.NotRunning:
                return False
            command = b"CONFIGURE\n" if self.registered else b"SETUP\n"
            return process.write(command) == len(command)
        except Exception:
            return False

    def stop(self) -> None:
        self.active = False
        self.portal_ready = False
        self.registered = False
        self.kde_enabled = False
        self.kde_trigger = ""
        process = self._process
        self._process = None

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


# Backward-compatible TUNE wrapper kept while Glass migrates to the generic
# shortcut runtime. Existing probes/tests and older callers continue to work.
GlobalTuneShortcutState = GlobalShortcutState


class XdgTuneGlobalShortcut(XdgGlobalShortcut):
    def __init__(
        self,
        *,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None] | None = None,
        on_state_change: Callable[[GlobalShortcutState], None] | None = None,
    ) -> None:
        super().__init__(
            shortcut_id="micro.tune.toggle",
            description="Toggle MICRO Processing (TUNE)",
            on_activate=on_activate,
            on_deactivate=on_deactivate,
            on_state_change=on_state_change,
        )
