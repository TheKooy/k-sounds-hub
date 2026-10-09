from pathlib import Path


def _source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "tools/global_shortcuts_helper/main.cpp").read_text(encoding="utf-8")


def test_helper_reads_kde_state_before_ever_binding():
    source = _source()
    start_pos = source.index("int start()")
    slots_pos = source.index("public slots:", start_pos)
    start_body = source[start_pos:slots_pos]
    assert "listShortcuts()" in start_body
    assert "bindShortcut()" not in start_body
    assert "KDE is authoritative" in start_body


def test_helper_listens_for_live_kde_shortcut_changes():
    source = _source()
    assert 'QStringLiteral("ShortcutsChanged")' in source
    assert "onShortcutsChanged(QDBusObjectPath,ShortcutSpecs)" in source
    assert '"SYNC\\t"' in source
    assert '\\tENABLED\\t' in source
    assert '\\tDISABLED\\t' in source
    assert '\\tMISSING\\t' in source


def test_helper_only_registers_missing_action_after_explicit_setup_command():
    source = _source()
    assert 'command == QStringLiteral("SETUP")' in source
    assert "bindShortcut();" in source
    assert 'command == QStringLiteral("CONFIGURE")' in source
    assert "configureShortcuts();" in source


def test_qsocketnotifier_connection_avoids_qt611_private_signal_pmf_trap():
    source = _source()
    assert "qOverload<QSocketDescriptor, QSocketNotifier::Type>" not in source
    assert "SIGNAL(activated(QSocketDescriptor,QSocketNotifier::Type))" in source
    assert "SLOT(onStdinActivated())" in source
    assert "void onStdinActivated()" in source
