from __future__ import annotations

from pathlib import Path

from ksound_hub.shortcuts_kglobalaccel import KdeGlobalShortcut


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_kglobalaccel_helper_uses_native_kde_api_and_shared_daemon_protocol():
    source = (_root() / "tools/kglobalaccel_helper/main.cpp").read_text(encoding="utf-8")
    assert "#include <KGlobalAccel>" in source
    assert "KGlobalAccel::Autoloading" in source
    assert "KGlobalAccel::NoAutoloading" in source
    assert 'command == QStringLiteral("REGISTER")' in source
    assert 'command == QStringLiteral("SET")' in source
    assert 'command == QStringLiteral("REFRESH")' in source
    assert 'QStringLiteral("daemon")' in source
    assert "isGlobalShortcutAvailable" in source
    assert '"CONFLICT\\t"' in source
    assert '"SET_OK\\t"' in source


def test_kglobalaccel_helper_build_links_framework():
    cmake = (_root() / "tools/kglobalaccel_helper/CMakeLists.txt").read_text(encoding="utf-8")
    assert "find_package(KF6GlobalAccel REQUIRED)" in cmake
    assert "KF6::GlobalAccel" in cmake


def test_kde_bridge_reports_inline_remap_and_syncs_trigger():
    states = []
    bridge = KdeGlobalShortcut(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        on_activate=lambda: None,
        on_state_change=states.append,
    )
    bridge._handle_hub_event("READY", ["READY", "micro.tune.toggle", "KGLOBALACCEL"])
    bridge._handle_hub_event("SYNC", ["SYNC", "micro.tune.toggle", "ENABLED", "F23"])
    state = bridge.state()
    assert state.portal_ready is True
    assert state.backend == "kglobalaccel"
    assert state.inline_remap is True
    assert state.kde_enabled is True
    assert state.kde_trigger == "F23"


def test_kde_bridge_keeps_conflict_visible_through_sync():
    bridge = KdeGlobalShortcut(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        on_activate=lambda: None,
    )
    bridge._handle_hub_event("READY", ["READY", "micro.tune.toggle", "KGLOBALACCEL"])
    bridge._handle_hub_event("SYNC", ["SYNC", "micro.tune.toggle", "ENABLED", "F23"])
    bridge._handle_hub_event("CONFLICT", ["CONFLICT", "micro.tune.toggle", "Ctrl+Alt+T", "Konsole"])
    bridge._handle_hub_event("SYNC", ["SYNC", "micro.tune.toggle", "ENABLED", "F23"])
    assert "already used" in bridge.state().detail
    assert bridge.state().kde_trigger == "F23"


def test_glass_uses_inline_setter_without_system_settings_button():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert "KdeGlobalShortcut" in source
    assert 'setter = getattr(bridge, "set_trigger", None)' in source
    assert "Configure in system shortcuts" not in source
    assert "globally automatically." in source


def test_arch_installer_knows_kglobalaccel_dependency_and_builds_helper():
    source = (_root() / "install.sh").read_text(encoding="utf-8")
    assert "kglobalaccel" in source
    assert "build_global_shortcut_helper" in source
    assert "build_kglobalaccel_helper.sh" in source


def test_glass_uses_single_chord_safe_recorder():
    source = (_root() / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
    assert "class ShortcutKeyEdit" in source
    assert "setMaximumSequenceLength" in source
    assert "Qt.Key_Return" in source
    assert "Qt.Key_Backspace" in source
    assert "shortcutCommitted" in source
    assert "Configure in system shortcuts" not in source


def test_helper_reports_conflict_owner_and_reclaims_only_same_logical_action():
    source = (_root() / "tools/kglobalaccel_helper/main.cpp").read_text(encoding="utf-8")
    assert "globalShortcutsByKey" in source
    assert "describeConflicts" in source
    assert "conflictIsOnlyThisLogicalAction" in source
    assert "KGlobalAccel::stealShortcutSystemwide(sequence)" in source
    assert '<< "CONFLICT\\t" << shortcutId << "\\t" << portable(sequence) << "\\t"' in source


def test_kde_bridge_formats_conflict_owner_for_ui():
    bridge = KdeGlobalShortcut(
        shortcut_id="micro.tune.toggle",
        description="Toggle MICRO Processing (TUNE)",
        on_activate=lambda: None,
    )
    bridge._handle_hub_event("READY", ["READY", "micro.tune.toggle", "KGLOBALACCEL"])
    bridge._handle_hub_event("SYNC", ["SYNC", "micro.tune.toggle", "ENABLED", "F23"])
    bridge._handle_hub_event("CONFLICT", ["CONFLICT", "micro.tune.toggle", "F22", "KWin — Some action"])
    state = bridge.state()
    assert state.kde_trigger == "F23"
    assert state.detail == "F22 is already used by KWin — Some action"


def test_helper_normalizes_legacy_multistep_sequences_and_reserves_edit_keys():
    source = (_root() / "tools/kglobalaccel_helper/main.cpp").read_text(encoding="utf-8")
    assert "parsed.count() > 1" in source
    assert "QKeySequence(parsed[0])" in source
    assert "reservedSequence" in source
    assert "Qt::Key_Return" in source
    assert "Qt::Key_Backspace" in source


def test_kglobalaccel_helper_has_gui_application_and_explicit_component_identity():
    source = (_root() / "tools/kglobalaccel_helper/main.cpp").read_text(encoding="utf-8")
    assert "#include <QGuiApplication>" in source
    assert "QGuiApplication app(argc, argv);" in source
    assert 'setProperty("componentName", m_appId)' in source
    assert 'setProperty("componentDisplayName", QStringLiteral("K-Sounds"))' in source
    assert 'QGuiApplication::setApplicationDisplayName(QStringLiteral("K-Sounds"))' in source


def test_python_backend_uses_one_shared_helper_process_for_all_actions():
    source = (_root() / "src/ksound_hub/shortcuts_kglobalaccel.py").read_text(encoding="utf-8")
    assert "class _KdeShortcutHub" in source
    assert "_SHARED_HUB = _KdeShortcutHub()" in source
    assert '["--daemon", "--app-id", "k-sounds-hub"]' in source
    assert '"REGISTER\\t{}\\t{}\\t{}"' in source


def test_shared_daemon_stdin_uses_qt611_safe_typed_connection_and_ping():
    source = (_root() / "tools/kglobalaccel_helper/main.cpp").read_text(encoding="utf-8")
    assert "&QSocketNotifier::activated" in source
    assert "[this](QSocketDescriptor, QSocketNotifier::Type)" in source
    assert 'command == QStringLiteral("PING")' in source
    assert '"PONG\\tKGLOBALACCEL"' in source
    assert 'SIGNAL(activated(QSocketDescriptor,QSocketNotifier::Type))' not in source


def test_python_hub_has_ready_watchdog_and_surfaces_daemon_errors():
    source = (_root() / "src/ksound_hub/shortcuts_kglobalaccel.py").read_text(encoding="utf-8")
    assert "def _startup_watchdog" in source
    assert "started but never became ready" in source
    assert 'if event == "ERROR"' in source
    assert 'if event == "PONG"' in source


def test_ready_and_set_ok_do_not_emit_transient_disabled_state():
    states = []
    bridge = KdeGlobalShortcut(
        shortcut_id="mixer.game.mute",
        description="GAME — Mute",
        on_activate=lambda: None,
        on_state_change=states.append,
    )
    bridge._handle_hub_event("READY", ["READY", "mixer.game.mute", "KGLOBALACCEL"])
    assert states == []
    bridge._handle_hub_event("SYNC", ["SYNC", "mixer.game.mute", "DISABLED", ""])
    assert len(states) == 1
    bridge._handle_hub_event("SET_OK", ["SET_OK", "mixer.game.mute", "F22"])
    assert len(states) == 1
    bridge._handle_hub_event("SYNC", ["SYNC", "mixer.game.mute", "ENABLED", "F22"])
    assert len(states) == 2
    assert states[-1].kde_trigger == "F22"
