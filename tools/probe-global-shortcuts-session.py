#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from ksound_hub.shortcuts_portal_session import (
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


def _object_path_text(value) -> str:
    value = unwrap_dbus_value(value)
    return str(value or "")


def main() -> int:
    try:
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Signal
        from PySide6.QtDBus import (
            QDBusAbstractInterface,
            QDBusConnection,
            QDBusInterface,
            QDBusMessage,
        )
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "stage": "import",
            "detail": f"QtDBus unavailable: {exc}",
        }, sort_keys=True))
        return 2

    class PortalRequest(QDBusAbstractInterface):
        # D-Bus signature: Response(u, a{sv})
        Response = Signal("uint", "QVariantMap")

        def __init__(self, path: str, bus, parent=None):
            super().__init__(
                PORTAL_SERVICE,
                path,
                REQUEST_INTERFACE,
                bus,
                parent,
            )

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    identity = resolve_portal_identity()

    if identity.desktop_file is None:
        print(json.dumps({
            "ok": False,
            "stage": "identity",
            "app_id": identity.app_id,
            "desktop_id": identity.desktop_id,
            "detail": "canonical k-sounds-hub.desktop was not found",
        }, sort_keys=True))
        return 3

    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        print(json.dumps({
            "ok": False,
            "stage": "session-bus",
            "app_id": identity.app_id,
            "detail": "D-Bus session bus unavailable",
        }, sort_keys=True))
        return 4

    registry = QDBusInterface(
        PORTAL_SERVICE,
        PORTAL_PATH,
        REGISTRY_INTERFACE,
        bus,
    )
    if not registry.isValid():
        print(json.dumps({
            "ok": False,
            "stage": "registry-interface",
            "app_id": identity.app_id,
            "detail": "Host Registry interface unavailable",
        }, sort_keys=True))
        return 5

    register_reply = registry.call("Register", identity.app_id, {})
    register_error = _message_error(register_reply)
    if register_error:
        print(json.dumps({
            "ok": False,
            "stage": "registry-register",
            "app_id": identity.app_id,
            "detail": register_error,
        }, sort_keys=True))
        return 6

    portal = QDBusInterface(
        PORTAL_SERVICE,
        PORTAL_PATH,
        PORTAL_INTERFACE,
        bus,
    )
    if not portal.isValid():
        print(json.dumps({
            "ok": False,
            "stage": "portal-interface",
            "app_id": identity.app_id,
            "registry_registered": True,
            "detail": "GlobalShortcuts interface unavailable",
        }, sort_keys=True))
        return 7

    handle_token = portal_token("ksounds_create")
    session_token = portal_token("ksounds_session")
    request_path = expected_request_path(bus.baseService(), handle_token)

    request = PortalRequest(request_path, bus, app)
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)

    state = {
        "received": False,
        "response": None,
        "results": {},
    }

    def on_response(response, results):
        state["received"] = True
        state["response"] = int(response)
        state["results"] = unwrap_dbus_value(dict(results))
        loop.quit()

    request.Response.connect(on_response)

    options = create_session_options(handle_token, session_token)
    create_reply = portal.call("CreateSession", options)
    create_error = _message_error(create_reply)
    if create_error:
        print(json.dumps({
            "ok": False,
            "stage": "create-call",
            "app_id": identity.app_id,
            "registry_registered": True,
            "request_path": request_path,
            "detail": create_error,
        }, sort_keys=True))
        return 8

    returned_request = ""
    try:
        args = create_reply.arguments()
        if args:
            returned_request = _object_path_text(args[0])
    except Exception:
        pass

    timeout.start(5000)
    loop.exec()

    if not state["received"]:
        print(json.dumps({
            "ok": False,
            "stage": "create-response",
            "app_id": identity.app_id,
            "registry_registered": True,
            "request_path": request_path,
            "returned_request": returned_request,
            "detail": "timed out waiting for org.freedesktop.portal.Request.Response",
        }, sort_keys=True))
        return 9

    response = int(state["response"])
    results = state["results"]
    session_handle = str(results.get("session_handle", "") or "")

    if response != 0 or not session_handle:
        print(json.dumps({
            "ok": False,
            "stage": "create-response",
            "app_id": identity.app_id,
            "registry_registered": True,
            "create_response": response,
            "request_path": request_path,
            "returned_request": returned_request,
            "results": results,
            "detail": "portal did not return a usable session_handle",
        }, sort_keys=True))
        return 10

    session = QDBusInterface(
        PORTAL_SERVICE,
        session_handle,
        SESSION_INTERFACE,
        bus,
    )
    close_error = ""
    if session.isValid():
        close_reply = session.call("Close")
        close_error = _message_error(close_reply)
    else:
        close_error = "Session interface was not valid"

    print(json.dumps({
        "ok": not bool(close_error),
        "stage": "complete" if not close_error else "session-close",
        "app_id": identity.app_id,
        "desktop_file": str(identity.desktop_file),
        "registry_registered": True,
        "create_response": response,
        "request_path": request_path,
        "returned_request": returned_request,
        "session_handle": session_handle,
        "session_closed": not bool(close_error),
        "detail": close_error or "host identity + CreateSession + Close succeeded",
    }, sort_keys=True))

    return 0 if not close_error else 11


if __name__ == "__main__":
    raise SystemExit(main())
