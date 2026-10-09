from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def test_ip13i2_build_marker_and_remote_ipc_bridge():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'def _handle_voice_fx_remote_command(self, payload: dict, command: str) -> bool:' in GLASS
    assert '"voice-fx-toggle"' in GLASS
    assert 'self.set_micro_voice_fx_state({"active": canonical})' in GLASS
    assert 'if effect == "telephone":' in GLASS
    assert 'active = [key for key in active if key != "radio"]' in GLASS
    assert 'elif effect in {"pitch_down", "pitch_up"}:' in GLASS
    assert 'elif effect in {"reverb", "church", "church_pa"}:' in GLASS


def test_remote_state_is_live_from_shared_settings_file():
    assert 'SETTINGS_PATH = CONFIG_DIR / "settings.json"' in WEB
    assert 'def read_voice_fx_state() -> dict:' in WEB
    assert 'state["voice_fx"] = read_voice_fx_state()' in WEB
    assert 'return _state_with_live_voice_fx(_STATE_CACHE_DOCUMENT)' in WEB


def test_remote_has_modular_pads_and_voice_surfaces():
    assert 'Remote IP10t · Hub IP' in WEB
    assert 'id="modulePadsButton"' in WEB
    assert 'id="moduleVoiceButton"' in WEB
    assert 'id="padsModule"' in WEB
    assert 'id="voiceFxModule"' in WEB
    assert 'function setRemoteMode(mode, persist = true)' in WEB
    assert 'window.localStorage.setItem("ksoundRemoteModule", wanted)' in WEB
    assert 'body.remote-mode-voice .bottom-bar' in WEB


def test_remote_voice_fx_is_state_synchronized_and_not_a_second_audio_system():
    assert 'function reconcileVoiceFxState(state)' in WEB
    assert 'async function toggleVoiceFx(button)' in WEB
    assert 'fetch("/voice-fx?token={escaped_token}"' in WEB
    assert 'send_ipc({"command": "voice-fx-toggle", "effect": effect, "enabled": enabled})' in WEB
    assert 'if (data && data.voice_fx) reconcileVoiceFxState(data.voice_fx);' in WEB
    assert 'micro_voice_fx_active' in WEB
    assert 'native_engine' not in WEB


def test_current_fun_and_historical_effect_catalog_is_exposed():
    for effect_id, label in (
        ("distortion", "Distortion"),
        ("pitch_down", "Pitch ↓"),
        ("pitch_up", "Pitch ↑"),
        ("radio", "Radio Pilot"),
        ("telephone", "Telephone"),
        ("robot", "Robot 1"),
        ("reverb", "Reverb"),
        ("church", "Church"),
        ("church_pa", "Church PA"),
    ):
        assert f'("{effect_id}", "{label}"' in WEB
