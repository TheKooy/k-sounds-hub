from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

from ksound_hub.audio_contract import AudioLayout, STEREO
from ksound_hub.audio_safety import (
    FINITE_GUARD_ABI,
    finite_guard_inputs,
    finite_guard_links_to,
    finite_or_silence,
    render_finite_guard_nodes,
)
from ksound_hub.builtin_modules.eq.runtime import build_eq_slot_signature, render_eq_dropin
from ksound_hub.models import EqProfile


def test_audio_layout_is_explicit_and_channel_count_agnostic() -> None:
    assert STEREO.channels == 2
    assert STEREO.positions == ("FL", "FR")
    assert STEREO.pipewire_positions == "[ FL FR ]"

    surround = AudioLayout(("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"))
    assert surround.channels == 8
    assert surround.pipewire_positions == "[ FL FR FC LFE RL RR SL SR ]"

    nodes = render_finite_guard_nodes(surround)
    assert nodes.count("type = ladspa") == 8
    assert 'name = finite_guard_7' in nodes
    assert finite_guard_inputs(surround).count("finite_guard_") == 8
    assert finite_guard_links_to(layout=surround, downstream_node="dsp").count("finite_guard_") == 8


def test_audio_layout_rejects_ambiguous_or_unsafe_positions() -> None:
    with pytest.raises(ValueError):
        AudioLayout(())
    with pytest.raises(ValueError):
        AudioLayout(("FL", "FL"))
    with pytest.raises(ValueError):
        AudioLayout(("FL", 'FR ] } malicious = true'))


def test_reference_finite_policy_is_not_a_limiter() -> None:
    finite_values = [0.0, -0.0, 0.5, -1.0, 1.25, -3.0, 1.0e30, 1.0e-40]
    for value in finite_values:
        result = finite_or_silence(value)
        assert result == value
        if value == 0.0:
            assert math.copysign(1.0, result) == math.copysign(1.0, value)

    assert finite_or_silence(float("nan")) == 0.0
    assert finite_or_silence(float("inf")) == 0.0
    assert finite_or_silence(float("-inf")) == 0.0


def test_eq_guard_is_before_stateful_dsp_and_signature_tracks_it() -> None:
    profile = EqProfile.default()
    rendered = render_eq_dropin(
        key="game",
        logical_sink="game",
        profile=profile,
        target_sink="test-output",
        stereo_width=100,
        layout=STEREO,
        finite_guard_active=True,
    )

    assert rendered.count("type = ladspa") == 2
    assert 'plugin = ksounds_finite_guard' in rendered
    assert 'label = ksounds_finite_guard_mono' in rendered
    assert 'output = "finite_guard_0:Output" input = "eq:In 1"' in rendered
    assert 'output = "finite_guard_1:Output" input = "eq:In 2"' in rendered
    assert 'inputs  = [ "finite_guard_0:Input" "finite_guard_1:Input" ]' in rendered
    assert rendered.index('name = finite_guard_0') < rendered.index('name = eq')
    assert 'audio.channels = 2' in rendered
    assert 'audio.position = [ FL FR ]' in rendered

    signature = json.loads(
        build_eq_slot_signature(
            key="game",
            profile=profile,
            target_label="test",
            target_sink="test-output",
            stereo_width=100,
            layout=STEREO,
            finite_guard_active=True,
        )
    )
    assert signature["audio_layout"] == {"sample_rate": 48000, "positions": ["FL", "FR"]}
    assert signature["finite_guard"] == {"active": True, "abi": FINITE_GUARD_ABI}


def test_current_eq_remains_intentionally_stereo() -> None:
    with pytest.raises(ValueError, match="supports stereo layouts only"):
        render_eq_dropin(
            key="game",
            logical_sink="game",
            profile=EqProfile.default(),
            target_sink="test-output",
            stereo_width=100,
            layout=AudioLayout(("FL", "FR", "FC", "LFE", "SL", "SR")),
            finite_guard_active=True,
        )


def test_float32_finite_bit_patterns_are_expected_to_be_preservable() -> None:
    # This is the exact invariant implemented by the LADSPA plugin's assignment:
    # finite input is copied, not bounded or rescaled.
    values = [0x00000000, 0x80000000, 0x3F000000, 0xBF800000, 0x3FA00000, 0x7F7FFFFF, 0x00000001]
    for bits in values:
        raw = struct.pack("<I", bits)
        value = struct.unpack("<f", raw)[0]
        assert math.isfinite(value)
        assert struct.pack("<f", value) == raw
