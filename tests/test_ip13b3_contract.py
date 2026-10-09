from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_build_tag_and_micro_card_cleanup_stays_present():
    text = read("src/ksound_hub/glass_app.py")
    assert 'APP_BUILD = "' in text
    assert ('QPushButton("TUNE", self)' in text or 'QPushButton("Tune", self)' in text)
    assert 'self.gate_btn = QPushButton("Hard Gate", self)' in text
    assert 'button.move(5, top_y)' in text
    assert 'voiceFxActive' in text
    assert 'set_voice_fx_active' in text


def test_generic_selectors_do_not_get_favorite_stars():
    text = read("src/ksound_hub/glass_app.py")
    assert 'favorite_mode = callable(self._favorite_getter) and callable(self._favorite_callback)' in text
    assert 'if favorite_mode:' in text
    assert 'favorite_other_label="Other presets"' in text


def test_micro_presets_have_real_favorites_and_global_shortcuts():
    models = read("src/ksound_hub/models.py")
    glass = read("src/ksound_hub/glass_app.py")
    assert 'favorite_micro_eq_presets: list[str]' in models
    assert 'def set_favorite_micro_eq_preset' in glass
    assert 'def micro_eq_preset_shortcut_id' in glass
    assert 'module="micro_preset"' in glass
    assert 'preset_shortcut_state_getter=self._micro_eq_preset_shortcut_state' in glass
    assert 'binding.module not in {"mixer", "soundboard", "micro_preset"}' in glass


def test_hard_gate_runtime_never_starts_legacy_worker():
    text = read("src/ksound_hub/glass_app.py")
    start = text.index('def _apply_micro_hard_gate_runtime(self) -> None:')
    end = text.index('def _micro_hard_gate_worker', start)
    block = text[start:end]
    assert '_micro_hard_gate_worker' not in block
    assert 'threading.Thread' not in block
    assert '_refresh_native_micro_hard_gate_runtime()' in block


def test_voice_fx_are_multi_toggle_with_robot1_and_fixed_chain():
    ui = read("src/ksound_hub/voice_fx_ui.py")
    native = read("native_engine/src/voice_fx_dsp.cpp")
    models = read("src/ksound_hub/models.py")
    assert '"Robot 1"' in ui
    assert 'self._apply({"active": list(self._active)})' in ui
    assert 'micro_voice_fx_active: list[str]' in models
    assert 'VoiceFxChainDsp' in native
    radio = native.index('apply_single_voice_fx(frames, dsp.radio')
    telephone = native.index('apply_single_voice_fx(frames, dsp.telephone')
    robot = native.index('apply_single_voice_fx(frames, dsp.robot')
    church = native.index('apply_single_voice_fx(frames, dsp.church')
    assert radio < telephone < robot < church
    assert 'Keep the validated IP13a Robot DSP unchanged' in native
    assert 'De-plosive the reverb send only.' in native
    assert 'effect == "church" || effect == "church_pa"' in native


def test_updated_regression_contracts_match_current_architecture():
    micro = read("tests/test_micro_processing_ui_contract.py")
    mixer = read("tests/test_shortcut_mixer_ip12l.py")
    sb = read("tests/test_soundboard_shortcuts_ip12t.py")
    detached = read("tests/test_detached_soundboard_visual_sync.py")
    assert 'VisibleCheckBox("Enable in K-Sounds")' in micro
    assert 'Global TUNE shortcut available' in micro
    assert 'if bool(getattr(bridge, "portal_ready", False)):' in micro
    assert 'QKeySequence(raw) if binding.enabled and raw else QKeySequence()' in micro
    assert 'favorite_other_label: str = "Other devices"' in mixer
    assert 'binding.module not in {"mixer", "soundboard", "micro_preset"}' in sb
    assert 'self._sync_shared_visual_settings(key)") == 8' in detached
