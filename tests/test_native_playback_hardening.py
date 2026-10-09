#!/usr/bin/env python3
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ksound_hub.audio import pipewire_v2_final as native_playback  # noqa: E402


def test_native_playback_remains_opt_in() -> None:
    engine = object.__new__(native_playback.PipeWireAudioEngine)
    engine._v2_fallback_reason = ""

    with mock.patch.dict(os.environ, {"KSH_NATIVE_PLAYBACK": "0"}, clear=False):
        assert not engine._native_playback_enabled()

    with mock.patch.dict(os.environ, {"KSH_NATIVE_PLAYBACK": "1"}, clear=False):
        assert engine._native_playback_enabled()
        engine._v2_fallback_reason = "test failure"
        assert not engine._native_playback_enabled()


def test_failure_budget_activates_safe_fallback() -> None:
    engine = object.__new__(native_playback.PipeWireAudioEngine)
    engine._v2_fallback_reason = ""
    engine._v2_engine_failures = []
    activated: list[str] = []
    engine._activate_v2_fallback = lambda reason, settings=None: activated.append(reason)

    with mock.patch.object(native_playback.time, "monotonic", side_effect=[1.0, 2.0, 3.0]):
        engine._record_v2_engine_failure("crash 1")
        engine._record_v2_engine_failure("crash 2")
        assert activated == []
        engine._record_v2_engine_failure("crash 3")

    assert activated == ["crash 3"]


def test_fallback_without_settings_is_fail_safe() -> None:
    engine = object.__new__(native_playback.PipeWireAudioEngine)
    engine._v2_fallback_reason = ""
    engine._v2_engine_proc = None
    engine._last_playback_settings = None
    engine.eq_slots = {"all": SimpleNamespace(status="native")}

    engine._activate_v2_fallback("binary missing")

    assert engine._v2_fallback_reason == "binary missing"
    assert engine.eq_slots["all"].status == "legacy playback safe fallback"


def test_cleanup_is_scoped_to_ksounds() -> None:
    python_cleanup = inspect.getsource(
        native_playback.PipeWireAudioEngine._kill_legacy_python_render
    )
    filter_cleanup = inspect.getsource(
        native_playback.PipeWireAudioEngine._kill_legacy_filter_chains
    )

    assert "--client-name=K-Sounds" in python_cleanup
    assert "--media-name=K-Sounds" in python_cleanup
    assert "_cleanup_stale_eq_filter_chain_processes" in filter_cleanup
    assert '["pgrep", "-af", "pipewire -c filter-chain.conf"]' not in filter_cleanup


def main() -> int:
    test_native_playback_remains_opt_in()
    test_failure_budget_activates_safe_fallback()
    test_fallback_without_settings_is_fail_safe()
    test_cleanup_is_scoped_to_ksounds()
    print("native playback hardening contract: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
