from pathlib import Path

from ksound_hub.models import AppSettings, ChannelConfig


def test_micro_processing_defaults_roundtrip_and_clamps():
    channel = ChannelConfig.from_dict({
        "key": "micro",
        "name": "MICRO",
        "micro_processing_enabled": True,
        "micro_input_gain_db": 99,
        "micro_noise_suppression": -5,
        "micro_compressor_enabled": True,
        "micro_compressor_amount": 150,
        "micro_deesser_enabled": True,
        "micro_deesser_amount": 101,
        "micro_limiter_enabled": False,
        "micro_limiter_ceiling_db": -99,
    })
    assert channel.micro_processing_enabled is True
    assert channel.micro_input_gain_db == 24.0
    assert channel.micro_noise_suppression == 0
    assert channel.micro_compressor_enabled is True
    assert channel.micro_compressor_amount == 100
    assert channel.micro_deesser_enabled is True
    assert channel.micro_deesser_amount == 100
    assert channel.micro_limiter_enabled is False
    assert channel.micro_limiter_ceiling_db == -12.0

    data = channel.to_dict()
    assert data["micro_processing_enabled"] is True
    for key in (
        "micro_processing_enabled",
        "micro_input_gain_db",
        "micro_noise_suppression",
        "micro_compressor_enabled",
        "micro_compressor_amount",
        "micro_deesser_enabled",
        "micro_deesser_amount",
        "micro_limiter_enabled",
        "micro_limiter_ceiling_db",
    ):
        assert key in data


def test_native_micro_state_contract_has_matching_keys():
    root = Path(__file__).resolve().parents[1]
    writer = (root / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
    engine = (root / "native_engine/src/micro_engine.cpp").read_text(encoding="utf-8")
    for key in (
        "processing_enabled",
        "input_gain_db",
        "noise_suppression",
        "compressor_enabled",
        "compressor_amount",
        "deesser_enabled",
        "deesser_amount",
        "limiter_enabled",
        "limiter_ceiling_db",
    ):
        assert key in writer
        assert f'parts[0] == "{key}"' in engine


def test_noise_suppression_enable_is_separate_from_strength():
    channel = ChannelConfig.from_dict({
        "key": "micro",
        "name": "MICRO",
        "micro_noise_suppression_enabled": False,
        "micro_noise_suppression": 67,
    })
    assert channel.micro_noise_suppression_enabled is False
    assert channel.micro_noise_suppression == 67
    data = channel.to_dict()
    assert data["micro_noise_suppression_enabled"] is False
    assert data["micro_noise_suppression"] == 67

    channel.micro_noise_suppression_enabled = True
    data = channel.to_dict()
    assert data["micro_noise_suppression_enabled"] is True
    assert data["micro_noise_suppression"] == 67


def test_ab_shortcut_roundtrip():
    settings = AppSettings.from_dict({
        "micro_processing_ab_shortcut": "Ctrl+Shift+B",
        "channels": [ChannelConfig(key="micro", name="MICRO").to_dict()],
    })
    assert settings.micro_processing_ab_shortcut == "Ctrl+Shift+B"
    assert settings.to_dict()["micro_processing_ab_shortcut"] == "Ctrl+Shift+B"


def test_ab_shortcut_enabled_roundtrip():
    settings = AppSettings.from_dict({
        "micro_processing_ab_shortcut": "Ctrl+Shift+B",
        "micro_processing_ab_shortcut_enabled": False,
        "channels": [ChannelConfig(key="micro", name="MICRO").to_dict()],
    })
    assert settings.micro_processing_ab_shortcut == "Ctrl+Shift+B"
    assert settings.micro_processing_ab_shortcut_enabled is False
    serialized = settings.to_dict()
    assert serialized["micro_processing_ab_shortcut"] == "Ctrl+Shift+B"
    assert serialized["micro_processing_ab_shortcut_enabled"] is False
