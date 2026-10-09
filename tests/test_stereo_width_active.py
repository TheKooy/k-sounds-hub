#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ksound_hub.audio.pipewire import (  # noqa: E402
    PipeWireAudioEngine,
    PlaybackTarget,
    _build_eq_slot_signature,
    _stereo_width_matrix,
)
from ksound_hub.models import EqProfile  # noqa: E402


def _process_frame(left: float, right: float, width: int) -> tuple[float, float]:
    same, cross = _stereo_width_matrix(width)
    return left * same + right * cross, right * same + left * cross


def test_width_matrix_is_quantitative() -> None:
    assert _stereo_width_matrix(-20) == (0.5, 0.5)
    assert _stereo_width_matrix(0) == (0.5, 0.5)
    assert _stereo_width_matrix(50) == (0.75, 0.25)
    assert _stereo_width_matrix(100) == (1.0, 0.0)
    assert _stereo_width_matrix(300) == (1.0, 0.0)

    assert _process_frame(1.0, -1.0, 0) == (0.0, 0.0)
    assert _process_frame(1.0, -1.0, 50) == (0.5, -0.5)
    assert _process_frame(1.0, -1.0, 100) == (1.0, -1.0)


def test_active_filter_chain_contains_width_matrix() -> None:
    engine = object.__new__(PipeWireAudioEngine)
    profile = EqProfile.default()

    mono = engine._render_eq_dropin(
        key="game",
        logical_sink="game",
        profile=profile,
        target_sink="test-output",
        stereo_width=0,
    )
    stereo = engine._render_eq_dropin(
        key="game",
        logical_sink="game",
        profile=profile,
        target_sink="test-output",
        stereo_width=100,
    )

    for rendered in (mono, stereo):
        assert 'name = width_left' in rendered
        assert 'name = width_right' in rendered
        assert 'output = "eq:Out 1" input = "width_left:In 1"' in rendered
        assert 'output = "eq:Out 2" input = "width_right:In 1"' in rendered
        assert 'outputs = [ "width_left:Out" "width_right:Out" ]' in rendered

    assert '"Gain 1" = 0.500000' in mono
    assert '"Gain 2" = 0.500000' in mono
    assert '"Gain 1" = 1.000000' in stereo
    assert '"Gain 2" = 0.000000' in stereo


def test_width_is_part_of_restart_signature() -> None:
    profile = EqProfile.default()
    target = PlaybackTarget(label="test-output", sink_name="test-output")
    mono = json.loads(_build_eq_slot_signature("game", profile, target, 0))
    stereo = json.loads(_build_eq_slot_signature("game", profile, target, 100))

    assert mono["stereo_width"] == 0
    assert stereo["stereo_width"] == 100
    assert mono != stereo


def main() -> int:
    test_width_matrix_is_quantitative()
    test_active_filter_chain_contains_width_matrix()
    test_width_is_part_of_restart_signature()
    print("stereo width active-path contract: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
