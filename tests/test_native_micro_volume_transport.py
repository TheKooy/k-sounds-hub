from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PY_ENGINE = ROOT / "src" / "ksound_hub" / "audio" / "pipewire_v2_final.py"
CPP_ENGINE = ROOT / "native_engine" / "src" / "micro_engine.cpp"


def test_native_micro_state_owns_the_user_volume() -> None:
    source = PY_ENGINE.read_text(encoding="utf-8")

    assert "micro_volume = int(channel.volume)" in source
    assert 'f"volume\\t{micro_volume}"' in source
    assert '"volume\\t100"' not in source


def test_native_micro_keeps_the_exported_endpoint_stable() -> None:
    source = PY_ENGINE.read_text(encoding="utf-8")

    assert "def _native_micro_transport_running" in source
    assert "self._native_micro_enabled() and self._native_micro_transport_running()" in source
    assert '["pactl", "set-source-volume", "micro", "100%"]' in source
    assert "PipeWireAudioEngineBase._apply_micro_endpoint_controls(self, channel)" in source


def test_native_micro_gain_changes_are_ramped_inside_the_transport() -> None:
    source = CPP_ENGINE.read_text(encoding="utf-8")

    assert "void mix_add_ramped(" in source
    assert "float& current_gain" in source
    assert "micro_gain_current" in source
    assert "mix_add_ramped(micro_mix, mic_chunk, micro_gain_current, mic_gain);" in source
