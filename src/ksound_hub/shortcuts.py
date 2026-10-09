from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping


ActivateCallback = Callable[[], None]
DeactivateCallback = Callable[[], None]


RESERVED_SHORTCUT_KEYS = frozenset({
    "return",
    "enter",
    "backspace",
    "delete",
    "escape",
    "tab",
    "backtab",
    "capslock",
    "caps lock",
    "numlock",
    "num lock",
    "scrolllock",
    "scroll lock",
    "mouse1",
    "mouse2",
    "mouse3",
    "left mouse button",
    "right mouse button",
    "middle mouse button",
})

MODIFIER_ONLY_SHORTCUT_KEYS = frozenset({
    "shift",
    "ctrl",
    "control",
    "alt",
    "meta",
    "super",
    "win",
    "altgr",
})


def shortcut_trigger_is_reserved(trigger: str) -> bool:
    """Reject editor-control keys, lock keys, mouse buttons and bare modifiers.

    Modifiers remain valid *inside* a normal chord (for example Ctrl+B).
    """
    text = str(trigger or "").strip()
    if not text:
        return False
    folded = text.casefold()
    if folded in MODIFIER_ONLY_SHORTCUT_KEYS:
        return True
    tokens = [part.strip().casefold() for part in text.split("+") if part.strip()]
    return any(token in RESERVED_SHORTCUT_KEYS for token in tokens)



def normalize_shortcut_trigger(trigger: str) -> str:
    text = str(trigger or "").strip()
    if shortcut_trigger_is_reserved(text):
        return ""
    return text


@dataclass(slots=True)
class ShortcutBinding:
    """Persistent/product-facing shortcut definition.

    `trigger` is deliberately stored as a string.  The UI/portal layer owns the
    translation between Qt key sequences and XDG GlobalShortcuts triggers.
    """

    shortcut_id: str
    description: str
    trigger: str = ""
    enabled: bool = True
    module: str = "core"
    allow_hold: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "shortcut_id": self.shortcut_id,
            "description": self.description,
            "trigger": self.trigger,
            "enabled": bool(self.enabled),
            "module": self.module,
            "allow_hold": bool(self.allow_hold),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ShortcutBinding":
        return cls(
            shortcut_id=str(data.get("shortcut_id", "") or "").strip(),
            description=str(data.get("description", "") or "").strip(),
            trigger=str(data.get("trigger", "") or "").strip(),
            enabled=bool(data.get("enabled", True)),
            module=str(data.get("module", "core") or "core").strip(),
            allow_hold=bool(data.get("allow_hold", False)),
        )


@dataclass(slots=True)
class ShortcutAction:
    binding: ShortcutBinding
    on_activate: ActivateCallback | None = None
    on_deactivate: DeactivateCallback | None = None
    active: bool = False


@dataclass(slots=True, frozen=True)
class ShortcutConflict:
    trigger: str
    shortcut_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class GlobalShortcutCapability:
    available: bool
    version: int = 0
    backend: str = "none"
    detail: str = ""


@dataclass(slots=True, frozen=True)
class ShortcutBackendState:
    """Runtime authority state reported by the desktop shortcut backend.

    ``authoritative`` means the desktop portal is live and its state must win
    over any in-app fallback. ``registered`` means KDE knows the action, while
    ``enabled`` means KDE currently has at least one trigger assigned to it.
    K-Sounds' persisted ``ShortcutBinding.enabled`` remains the secondary,
    application-level enable switch.
    """

    authoritative: bool = False
    registered: bool = False
    enabled: bool = False
    trigger_description: str = ""
    detail: str = ""


class ShortcutRegistry:
    """Single source of truth for every K-Sounds shortcut.

    Product modules register actions here.  Settings and module-local editors
    should both edit the same bindings rather than maintaining separate copies.
    """

    def __init__(self) -> None:
        self._actions: dict[str, ShortcutAction] = {}

    def register(
        self,
        binding: ShortcutBinding,
        *,
        on_activate: ActivateCallback | None = None,
        on_deactivate: DeactivateCallback | None = None,
        replace: bool = False,
    ) -> ShortcutAction:
        shortcut_id = binding.shortcut_id.strip()
        if not shortcut_id:
            raise ValueError("shortcut_id must not be empty")
        if shortcut_id in self._actions and not replace:
            raise ValueError(f"shortcut already registered: {shortcut_id}")

        binding.shortcut_id = shortcut_id
        action = ShortcutAction(
            binding=binding,
            on_activate=on_activate,
            on_deactivate=on_deactivate,
        )
        self._actions[shortcut_id] = action
        return action

    def unregister(self, shortcut_id: str) -> None:
        self._actions.pop(shortcut_id, None)

    def get(self, shortcut_id: str) -> ShortcutAction | None:
        return self._actions.get(shortcut_id)

    def require(self, shortcut_id: str) -> ShortcutAction:
        action = self.get(shortcut_id)
        if action is None:
            raise KeyError(shortcut_id)
        return action

    def actions(self) -> tuple[ShortcutAction, ...]:
        return tuple(self._actions.values())

    def bindings(self) -> tuple[ShortcutBinding, ...]:
        return tuple(action.binding for action in self._actions.values())

    def enabled_bindings(self) -> tuple[ShortcutBinding, ...]:
        return tuple(
            action.binding
            for action in self._actions.values()
            if action.binding.enabled and bool(action.binding.trigger.strip())
        )

    def set_binding(
        self,
        shortcut_id: str,
        *,
        trigger: str | None = None,
        enabled: bool | None = None,
    ) -> ShortcutBinding:
        action = self.require(shortcut_id)
        if trigger is not None:
            action.binding.trigger = str(trigger).strip()
        if enabled is not None:
            action.binding.enabled = bool(enabled)
            if not action.binding.enabled and action.active:
                self.deactivate(shortcut_id)
        return action.binding

    def activate(self, shortcut_id: str) -> bool:
        action = self.get(shortcut_id)
        if action is None:
            return False
        binding = action.binding
        if not binding.enabled or not binding.trigger.strip():
            return False

        # Prevent auto-repeat from toggling actions multiple times. Hold-capable
        # actions become active once and remain active until Deactivated arrives.
        if action.active:
            return False

        action.active = True
        if action.on_activate is not None:
            action.on_activate()
        return True

    def deactivate(self, shortcut_id: str) -> bool:
        action = self.get(shortcut_id)
        if action is None or not action.active:
            return False

        action.active = False
        if action.binding.allow_hold and action.on_deactivate is not None:
            action.on_deactivate()
        return True

    def conflicts(self) -> tuple[ShortcutConflict, ...]:
        by_trigger: dict[str, list[str]] = {}
        for binding in self.enabled_bindings():
            key = binding.trigger.casefold()
            by_trigger.setdefault(key, []).append(binding.shortcut_id)

        conflicts: list[ShortcutConflict] = []
        for key, shortcut_ids in by_trigger.items():
            if len(shortcut_ids) < 2:
                continue
            first = self.require(shortcut_ids[0]).binding.trigger
            conflicts.append(
                ShortcutConflict(
                    trigger=first,
                    shortcut_ids=tuple(shortcut_ids),
                )
            )
        return tuple(conflicts)

    def export_bindings(self) -> list[dict[str, object]]:
        return [binding.to_dict() for binding in self.bindings()]

    def apply_saved_bindings(self, values: Iterable[Mapping[str, object]]) -> None:
        for raw in values:
            saved = ShortcutBinding.from_dict(raw)
            action = self.get(saved.shortcut_id)
            if action is None:
                continue
            action.binding.trigger = normalize_shortcut_trigger(saved.trigger)
            action.binding.enabled = bool(saved.enabled and action.binding.trigger)


class ShortcutManager:
    """Backend-neutral shortcut dispatch owned by K-Sounds Core.

    The XDG portal backend will feed Activated/Deactivated events into this
    object.  In-app fallback shortcuts can feed the exact same methods.
    """

    def __init__(self, registry: ShortcutRegistry | None = None) -> None:
        self.registry = registry or ShortcutRegistry()
        self._backend_states: dict[str, ShortcutBackendState] = {}

    def set_backend_state(
        self,
        shortcut_id: str,
        *,
        authoritative: bool,
        registered: bool = False,
        enabled: bool = False,
        trigger_description: str = "",
        detail: str = "",
    ) -> ShortcutBackendState:
        state = ShortcutBackendState(
            authoritative=bool(authoritative),
            registered=bool(registered),
            enabled=bool(enabled),
            trigger_description=str(trigger_description or "").strip(),
            detail=str(detail or ""),
        )
        self._backend_states[str(shortcut_id)] = state
        if state.authoritative and not state.enabled:
            self.registry.deactivate(shortcut_id)
        return state

    def backend_state(self, shortcut_id: str) -> ShortcutBackendState:
        return self._backend_states.get(str(shortcut_id), ShortcutBackendState())

    def can_activate(self, shortcut_id: str) -> bool:
        action = self.registry.get(shortcut_id)
        if action is None:
            return False
        if not action.binding.enabled or not action.binding.trigger.strip():
            return False
        backend = self.backend_state(shortcut_id)
        if backend.authoritative and not backend.enabled:
            return False
        return True

    def activated(self, shortcut_id: str) -> bool:
        if not self.can_activate(shortcut_id):
            return False
        return self.registry.activate(shortcut_id)

    def deactivated(self, shortcut_id: str) -> bool:
        return self.registry.deactivate(shortcut_id)

    def conflicts(self) -> tuple[ShortcutConflict, ...]:
        return self.registry.conflicts()


def probe_xdg_global_shortcuts() -> GlobalShortcutCapability:
    """Best-effort capability probe without making QtDBus a hard import.

    K-Sounds already depends on PySide6.  The actual portal session/bind layer
    will live behind this boundary.  A missing QtDBus module or portal must
    degrade cleanly to the in-app fallback rather than preventing startup.
    """

    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except Exception as exc:  # pragma: no cover - host dependent
        return GlobalShortcutCapability(
            available=False,
            detail=f"QtDBus unavailable: {exc}",
        )

    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return GlobalShortcutCapability(
                available=False,
                backend="xdg-portal",
                detail="D-Bus session bus unavailable",
            )

        iface = QDBusInterface(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.GlobalShortcuts",
            bus,
        )
        if not iface.isValid():
            return GlobalShortcutCapability(
                available=False,
                backend="xdg-portal",
                detail="GlobalShortcuts portal interface unavailable",
            )

        raw_version = iface.property("version")
        try:
            version = int(raw_version)
        except Exception:
            version = 0

        return GlobalShortcutCapability(
            available=True,
            version=version,
            backend="xdg-portal",
            detail="org.freedesktop.portal.GlobalShortcuts",
        )
    except Exception as exc:  # pragma: no cover - host dependent
        return GlobalShortcutCapability(
            available=False,
            backend="xdg-portal",
            detail=str(exc),
        )
