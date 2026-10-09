from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
VFX = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")


def test_ip13f_build_marker_family():
    assert 'APP_BUILD = "IP' in GLASS


def test_pads_and_vfx_share_balanced_monitoring_action_row():
    assert 'def _add_balanced_dashboard_action_row(' in GLASS
    soundboard = GLASS.split('def _build_compact_soundboard_dashboard', 1)[1].split('def __init__', 1)[0]
    vfx = GLASS.split('def _voice_fx_page(self)', 1)[1].split('def _sync_voice_fx_monitoring', 1)[0]
    assert '_add_balanced_dashboard_action_row(' in soundboard
    assert '_add_balanced_dashboard_action_row(' in vfx
    assert 'open_button = QPushButton("Open")' in vfx


def test_vfx_page_matches_soundboard_header_rhythm_and_compact_monitoring():
    vfx = GLASS.split('def _voice_fx_page(self)', 1)[1].split('def _sync_voice_fx_monitoring', 1)[0]
    assert 'header.setObjectName("sectionHeader")' in vfx
    assert 'title = QLabel("Voice FX")' in vfx
    assert 'self.voice_fx_status_badge = QLabel("Off")' in vfx
    assert 'self.voice_fx_monitor_active.setObjectName("voiceFxStatusPill")' in vfx
    assert 'self.voice_fx_monitor_strength = QLabel("Strength 0%")' in vfx
    assert 'root.addWidget(editor_shell)' in vfx
    assert 'root.addStretch(1)' in vfx


def test_embedded_vfx_drops_redundant_intro_but_detached_keeps_help():
    assert 'if not self._embedded:' in VFX
    assert 'title = QLabel("Voice FX · native low-latency")' in VFX
    assert 'preset_label = QLabel("Effects")' in VFX
    embedded_branch = VFX.split('if not self._embedded:', 1)[0]
    assert 'Toggle effects on the physical MICRO input.' not in embedded_branch


def test_vfx_monitor_badge_uses_compact_summary_for_stacking():
    sync = GLASS.split('def _sync_voice_fx_monitoring(self, state: dict)', 1)[1].split('def _detach_voice_fx', 1)[0]
    assert 'compact_active_text' in sync
    assert 'f"{len(active_names)} effects"' in sync
    assert 'badge.setText(f"{len(active_names)} active")' in sync
