from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPEWIRE = (ROOT / "src/ksound_hub/audio/pipewire.py").read_text(encoding="utf-8")
V2 = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")
RUNTIME = (ROOT / "src/ksound_hub/builtin_modules/eq/runtime.py").read_text(encoding="utf-8")
LIFECYCLE = (ROOT / "src/ksound_hub/builtin_modules/eq/lifecycle.py").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "src/ksound_hub/builtin_modules/eq/module.json").read_text(encoding="utf-8"))
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")


def test_ip14i_is_core_refactor_without_eq_version_bump():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'Hub IP14' in WEB
    assert MANIFEST["version"] == "1.0.0"
    assert MANIFEST["ksounds_api"] == 1


def test_eq_owns_definition_and_lifecycle_files():
    owned = set(MANIFEST["owned_paths"])
    assert "src/ksound_hub/builtin_modules/eq/runtime.py" in owned
    assert "src/ksound_hub/builtin_modules/eq/lifecycle.py" in owned
    assert "class EqRuntimeSlot:" in RUNTIME
    assert "def render_eq_dropin(" in RUNTIME
    assert "class EqProcessLifecycle:" in LIFECYCLE
    assert "class AdoptedEqProcess:" in LIFECYCLE
    assert "def start_slot(" in LIFECYCLE
    assert "def adopt_preserved_runtimes(" in LIFECYCLE


def test_core_pipewire_lifecycle_methods_are_thin_bridges():
    assert "self._eq_lifecycle = EqProcessLifecycle(" in PIPEWIRE
    assert "self._eq_lifecycle.start_slot(" in PIPEWIRE
    assert "self._eq_lifecycle.stop_slot(slot)" in PIPEWIRE
    assert "self._eq_lifecycle.cleanup_stale_filter_chain_processes()" in PIPEWIRE
    assert "self._eq_lifecycle.write_slot_dropin(key, text)" in PIPEWIRE
    assert "Make before break" not in PIPEWIRE


def test_v2_preserved_runtime_adoption_delegates_to_eq_lifecycle():
    assert "AdoptedEqProcess as _AdoptedEqProcess" in V2
    assert "self._eq_lifecycle.preserved_runtime_pids(" in V2
    assert "self._eq_lifecycle.adopt_preserved_runtimes(" in V2
    assert "class _AdoptedEqProcess:" not in V2


def test_no_cut_constants_and_make_before_break_are_eq_owned():
    assert "EQ_HOT_SWAP_DUCK_FACTOR = 0.80" in LIFECYCLE
    assert "EQ_HOT_SWAP_READY_TIMEOUT_SECONDS = 0.09" in LIFECYCLE
    assert "previous_proc = slot.proc" in LIFECYCLE
    assert "new_proc = self.spawn_process(slot)" in LIFECYCLE
    assert "self.wait_for_playback_stream" in LIFECYCLE
    assert "self.terminate_process(previous_proc)" in LIFECYCLE
    assert "self.set_sink_volume(slot.logical_sink, handover_volume)" in LIFECYCLE


if __name__ == "__main__":
    test_ip14i_is_core_refactor_without_eq_version_bump()
    test_eq_owns_definition_and_lifecycle_files()
    test_core_pipewire_lifecycle_methods_are_thin_bridges()
    test_v2_preserved_runtime_adoption_delegates_to_eq_lifecycle()
    test_no_cut_constants_and_make_before_break_are_eq_owned()
    print("IP14i EQ lifecycle split contracts: passed")
