from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_model_persists_native_voice_fx_state():
    text = read("src/ksound_hub/models.py")
    assert 'micro_voice_fx_enabled: bool = False' in text
    assert 'micro_voice_fx_preset: str = "radio"' in text
    assert 'micro_voice_fx_amount: int = 72' in text
    assert '"micro_voice_fx_enabled"' in text
    assert '"micro_voice_fx_preset"' in text
    assert '"micro_voice_fx_amount"' in text


def test_native_state_carries_voice_fx_without_route_rebuild():
    text = read("src/ksound_hub/audio/pipewire_v2_final.py")
    assert '"version\\t' in text
    assert 'voice_fx_enabled\\t' in text
    assert 'voice_fx_preset\\t' in text
    assert 'voice_fx_active\\t' in text
    assert 'voice_fx_amount\\t' in text


def test_native_engine_has_four_voice_fx_and_probe():
    engine = read("native_engine/src/micro_engine.cpp")
    dsp = read("native_engine/src/voice_fx_dsp.cpp")
    assert 'void apply_voice_fx(' in dsp
    assert 'voice_fx_active(state, "radio")' in dsp
    assert 'voice_fx_active(state, "telephone")' in dsp
    assert 'voice_fx_active(state, "church")' in dsp
    assert 'voice_fx_active(state, "robot")' in dsp
    assert '--probe-voice-fx' in engine
    assert 'micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);' in engine

def test_voice_fx_only_precedes_physical_micro_mix():
    text = read("native_engine/src/micro_engine.cpp")
    apply_pos = text.index('micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);')
    physical_mix_pos = text.index('mix_add_ramped(micro_mix, mic_chunk, micro_gain_current, mic_gain);', apply_pos)
    send_loop_pos = text.index('for (const auto& send : state.sends)', physical_mix_pos)
    assert apply_pos < physical_mix_pos < send_loop_pos

def test_glass_exposes_voice_fx_dialog_and_live_backend():
    text = read("src/ksound_hub/glass_app.py")
    assert 'from .voice_fx_ui import VoiceFxDialog' in text
    assert 'def micro_voice_fx_state(self) -> dict:' in text
    assert 'def set_micro_voice_fx_state(self, updates: dict) -> bool:' in text
    assert 'self._refresh_native_micro_hard_gate_runtime()' in text
    assert 'voice_fx_callback=self._open_micro_voice_fx_dialog' in text


def test_startup_soundboard_reconcile_is_bounded_not_per_pad():
    text = read("src/ksound_hub/glass_app.py")
    assert 'def _schedule_soundboard_startup_reconcile(self) -> None:' in text
    assert 'for delay_ms in (280, 950):' in text
    shortcut_start = text.index('def play_soundboard_slot_shortcut')
    shortcut_end = text.index('def set_soundboard_micro_enabled', shortcut_start)
    shortcut_block = text[shortcut_start:shortcut_end]
    assert '_reconcile_soundboard_startup_routes' not in shortcut_block
    assert 'apply_glass_runtime_routes' not in shortcut_block


def test_voice_fx_ui_keeps_tune_independent():
    text = read("src/ksound_hub/voice_fx_ui.py")
    assert ('TUNE remains independent' in text or 'Tune remains independent' in text)
    assert 'Radio Pilot' in text
    assert 'Telephone' in text
    assert 'Church' in text
    assert 'Robot' in text
