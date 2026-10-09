from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
MODELS = (ROOT / "src/ksound_hub/models.py").read_text(encoding="utf-8")
VFX_UI = (ROOT / "src/ksound_hub/voice_fx_ui.py").read_text(encoding="utf-8")
NATIVE = (ROOT / "native_engine/src/voice_fx_dsp.cpp").read_text(encoding="utf-8")


def test_build_marker_is_visible_in_title_bar():
    assert 'APP_BUILD = "' in GLASS
    assert 'version = QLabel(APP_BUILD)' in GLASS
    assert 'version.setObjectName("versionText")' in GLASS


def test_voice_fx_card_halo_is_in_dynamic_glass_style():
    start = GLASS.index("    def _apply_visual_style(self) -> None:")
    end = GLASS.index("    def _sync_channel_cards_from_settings", start)
    block = GLASS[start:end]
    assert 'QFrame#channelCard[voiceFxActive="true"]' in block
    assert 'QFrame#channelCard[voiceFxActive="true"]:hover' in block
    assert 'border: 1px solid rgba(255, 207, 66, 100);' in block


def test_tune_and_vfx_buttons_have_subtle_blue_idle_style():
    assert 'background: rgba(42, 112, 156, 34);' in GLASS
    assert 'border: 1px solid rgba(92, 190, 238, 34);' in GLASS
    assert ('self.fx_btn = QPushButton("TUNE", self)' in GLASS or 'self.fx_btn = QPushButton("Tune", self)' in GLASS)
    assert 'self.voice_fx_btn = QPushButton("VFX", self)' in GLASS


def test_micro_and_output_eq_favorites_are_persisted_separately():
    assert 'favorite_micro_eq_presets: list[str] = field(default_factory=list)' in MODELS
    assert 'favorite_output_eq_presets: list[str] = field(default_factory=list)' in MODELS
    assert '"favorite_micro_eq_presets": list(self.favorite_micro_eq_presets)' in MODELS
    assert '"favorite_output_eq_presets": list(self.favorite_output_eq_presets)' in MODELS
    assert 'def favorite_micro_eq_presets(self) -> list[str]:' in GLASS
    assert 'def favorite_output_eq_presets(self) -> list[str]:' in GLASS
    assert 'def set_favorite_micro_eq_preset(self, name: str, favorite: bool) -> bool:' in GLASS
    assert 'def set_favorite_output_eq_preset(self, name: str, favorite: bool) -> bool:' in GLASS


def test_eq_preset_selectors_enable_favorites_without_generic_combo_leakage():
    assert 'favorite_getter=self._preset_is_favorite' in GLASS
    assert 'favorite_callback=self._set_preset_favorite' in GLASS
    assert 'if self._current_channel_key() == "micro":' in GLASS
    # Generic SelectButton remains opt-in: stars only appear when both callbacks exist.
    assert 'favorite_mode = callable(self._favorite_getter) and callable(self._favorite_callback)' in GLASS


def test_robot_1_dsp_remains_the_validated_chain():
    assert 'constexpr float carrier_hz = 68.0f;' in NATIVE
    assert 'wet[index] = std::tanh(sample * 1.8f) * carrier * 0.90f;' in NATIVE
    assert 'Keep the validated IP13a Robot DSP unchanged: this is Robot 1.' in NATIVE


def test_telephone_is_now_true_narrowband_line_simulation():
    assert '8 kHz sample-and-hold' in NATIVE
    assert 'dsp.decimation_phase = (dsp.decimation_phase + 1) % 6;' in NATIVE
    assert 'voice_fx_highpass(sample, 430.0f' in NATIVE
    assert 'std::round(sample * 44.0f) / 44.0f' in NATIVE
    assert 'Old-handset narrowband voice with obvious 8 kHz line character.' in VFX_UI


def test_radio_is_strengthened_without_sharing_telephone_chain():
    assert 'voice_fx_highpass(sample, 360.0f' in NATIVE
    assert 'voice_fx_lowpass(sample, 3150.0f' in NATIVE
    assert 'std::tanh(sample * 5.0f) / std::tanh(5.0f)' in NATIVE
    assert 'texture * 0.014f' in NATIVE


def test_church_uses_spread_reflections_and_damped_tail():
    assert 'predelay[ch].assign(static_cast<std::size_t>(2591 + ch * 149)' in NATIVE
    assert 'comb_d[ch].assign(static_cast<std::size_t>(3371 + ch * 53)' in NATIVE
    assert 'voice_fx_lowpass(tail, pa_mode ? 4400.0f : 5100.0f' in NATIVE
    assert 'De-plosive the reverb send only.' in NATIVE
    assert 'effect == "church" || effect == "church_pa"' in NATIVE
