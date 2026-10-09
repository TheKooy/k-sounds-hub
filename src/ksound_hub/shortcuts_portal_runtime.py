from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .shortcuts_portal_session import (
    PORTAL_INTERFACE,
    PORTAL_PATH,
    PORTAL_SERVICE,
    REGISTRY_INTERFACE,
    REQUEST_INTERFACE,
    SESSION_INTERFACE,
    create_session_options,
    expected_request_path,
    portal_token,
    resolve_portal_identity,
    unwrap_dbus_value,
)


@dataclass(slots=True, frozen=True)
class PortalRequestResult:
    response: int
    results: dict[str, Any]


@dataclass(slots=True, frozen=True)
class PortalRuntimeSnapshot:
    ready: bool
    app_id: str
    session_handle: str = ""
    detail: str = ""


def list_shortcuts_options(handle_token: str) -> dict[str, str]:
    token = str(handle_token or "").strip()
    if not token:
        raise ValueError("handle_token must not be empty")
    return {"handle_token": token}


def _message_error(message) -> str:
    try:
        if message.type() == message.MessageType.ErrorMessage:
            return f"{message.errorName()}: {message.errorMessage()}"
    except Exception:
        pass
    try:
        if message.errorName():
            return f"{message.errorName()}: {message.errorMessage()}"
    except Exception:
        pass
    return ""


class XdgGlobalShortcutsRuntime:
    """Long-lived host-side GlobalShortcuts session helper.

    IP12d intentionally stops at CreateSession + ListShortcuts.  BindShortcuts
    and signal dispatch are layered on this exact runtime in the next milestone.
    """

    def __init__(self) -> None:
        self.identity = resolve_portal_identity()
        self.bus = None
        self.portal = None
        self.registry = None
        self.session = None
        self.session_handle = ""
        self.registry_registered = False
        # Standalone probes/tools do not already have a Qt application object.
        # Keep an owned QCoreApplication alive for the lifetime of this runtime
        # when K-Sounds itself did not create one.
        self._owned_core_app = None

    def snapshot(self) -> PortalRuntimeSnapshot:
        return PortalRuntimeSnapshot(
            ready=bool(self.session_handle),
            app_id=self.identity.app_id,
            session_handle=self.session_handle,
            detail="ready" if self.session_handle else "not started",
        )

    def _qt(self):
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Signal
        from PySide6.QtDBus import (
            QDBusAbstractInterface,
            QDBusConnection,
            QDBusInterface,
            QDBusObjectPath,
        )
        return {
            "QCoreApplication": QCoreApplication,
            "QEventLoop": QEventLoop,
            "QTimer": QTimer,
            "Signal": Signal,
            "QDBusAbstractInterface": QDBusAbstractInterface,
            "QDBusConnection": QDBusConnection,
            "QDBusInterface": QDBusInterface,
            "QDBusObjectPath": QDBusObjectPath,
        }

    def _request_class(self, qt):
        QDBusAbstractInterface = qt["QDBusAbstractInterface"]
        Signal = qt["Signal"]

        class PortalRequest(QDBusAbstractInterface):
            Response = Signal("uint", "QVariantMap")

            def __init__(self, path: str, bus, parent=None):
                super().__init__(
                    PORTAL_SERVICE,
                    path,
                    REQUEST_INTERFACE,
                    bus,
                    parent,
                )

        return PortalRequest

    def _wait_for_request(self, request, *, timeout_ms: int = 5000) -> PortalRequestResult:
        qt = self._qt()
        QEventLoop = qt["QEventLoop"]
        QTimer = qt["QTimer"]

        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        state: dict[str, Any] = {
            "received": False,
            "response": None,
            "results": {},
        }

        def done(response, results):
            state["received"] = True
            state["response"] = int(response)
            state["results"] = unwrap_dbus_value(dict(results))
            loop.quit()

        request.Response.connect(done)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        loop.exec()

        if not state["received"]:
            raise TimeoutError("timed out waiting for portal Request.Response")

        return PortalRequestResult(
            response=int(state["response"]),
            results=dict(state["results"]),
        )

    def register_host(self) -> None:
        qt = self._qt()
        QDBusConnection = qt["QDBusConnection"]
        QDBusInterface = qt["QDBusInterface"]

        if self.identity.desktop_file is None:
            raise RuntimeError("canonical k-sounds-hub.desktop was not found")

        self.bus = QDBusConnection.sessionBus()
        if not self.bus.isConnected():
            raise RuntimeError("D-Bus session bus unavailable")

        self.registry = QDBusInterface(
            PORTAL_SERVICE,
            PORTAL_PATH,
            REGISTRY_INTERFACE,
            self.bus,
        )
        if self.registry.isValid():
            reply = self.registry.call("Register", self.identity.app_id, {})
            error = _message_error(reply)
            if error:
                # Registry.Register is at-most-once per peer. A runtime instance
                # calls this once; exposing the exact error is safer than hiding
                # an identity mismatch.
                raise RuntimeError(error)
            self.registry_registered = True

        self.portal = QDBusInterface(
            PORTAL_SERVICE,
            PORTAL_PATH,
            PORTAL_INTERFACE,
            self.bus,
        )
        if not self.portal.isValid():
            raise RuntimeError("GlobalShortcuts portal interface unavailable")

    def create_session(self) -> str:
        if self.portal is None or self.bus is None:
            self.register_host()

        qt = self._qt()
        QCoreApplication = qt["QCoreApplication"]
        PortalRequest = self._request_class(qt)

        app = QCoreApplication.instance()
        if app is None:
            self._owned_core_app = QCoreApplication([])
            app = self._owned_core_app

        handle_token = portal_token("ksounds_create")
        session_token = portal_token("ksounds_session")
        request_path = expected_request_path(self.bus.baseService(), handle_token)
        request = PortalRequest(request_path, self.bus, app)

        # Subscribe before the portal call: fast backends are allowed to answer
        # immediately.
        response_state: dict[str, Any] = {}
        original_wait = self._wait_for_request

        # _wait_for_request installs its signal handler. To guarantee it is
        # connected before the method call, connect a small capture here and
        # reuse a local event loop below instead of calling original_wait.
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        def done(response, results):
            response_state["response"] = int(response)
            response_state["results"] = unwrap_dbus_value(dict(results))
            loop.quit()

        request.Response.connect(done)

        reply = self.portal.call(
            "CreateSession",
            create_session_options(handle_token, session_token),
        )
        error = _message_error(reply)
        if error:
            raise RuntimeError(error)

        timer.timeout.connect(loop.quit)
        timer.start(5000)
        loop.exec()

        if "response" not in response_state:
            raise TimeoutError("timed out waiting for CreateSession response")
        if int(response_state["response"]) != 0:
            raise RuntimeError(
                f"CreateSession portal response={response_state['response']}"
            )

        handle = str(
            dict(response_state["results"]).get("session_handle", "") or ""
        )
        if not handle:
            raise RuntimeError("CreateSession returned no session_handle")

        QDBusInterface = qt["QDBusInterface"]
        self.session_handle = handle
        self.session = QDBusInterface(
            PORTAL_SERVICE,
            handle,
            SESSION_INTERFACE,
            self.bus,
        )
        return handle

    def list_shortcuts(self) -> PortalRequestResult:
        if not self.session_handle:
            self.create_session()

        qt = self._qt()
        QCoreApplication = qt["QCoreApplication"]
        QEventLoop = qt["QEventLoop"]
        QTimer = qt["QTimer"]
        QDBusObjectPath = qt["QDBusObjectPath"]
        PortalRequest = self._request_class(qt)

        token = portal_token("ksounds_list")
        request_path = expected_request_path(self.bus.baseService(), token)
        request = PortalRequest(
            request_path,
            self.bus,
            QCoreApplication.instance(),
        )

        state: dict[str, Any] = {}
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        def done(response, results):
            state["response"] = int(response)
            state["results"] = unwrap_dbus_value(dict(results))
            loop.quit()

        request.Response.connect(done)

        reply = self.portal.call(
            "ListShortcuts",
            QDBusObjectPath(self.session_handle),
            list_shortcuts_options(token),
        )
        error = _message_error(reply)
        if error:
            raise RuntimeError(error)

        timer.timeout.connect(loop.quit)
        timer.start(5000)
        loop.exec()

        if "response" not in state:
            raise TimeoutError("timed out waiting for ListShortcuts response")

        return PortalRequestResult(
            response=int(state["response"]),
            results=dict(state["results"]),
        )

    def close(self) -> None:
        if self.session is not None and self.session.isValid():
            reply = self.session.call("Close")
            error = _message_error(reply)
            if error:
                raise RuntimeError(error)
        self.session = None
        self.session_handle = ""

    def __enter__(self):
        self.create_session()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
        return False
