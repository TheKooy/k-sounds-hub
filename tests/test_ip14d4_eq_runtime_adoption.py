from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GLASS_PATH = ROOT / "src/ksound_hub/glass_app.py"
V2_PATH = ROOT / "src/ksound_hub/audio/pipewire_v2_final.py"
BASE_PATH = ROOT / "src/ksound_hub/audio/pipewire.py"
LIFECYCLE_PATH = ROOT / "src/ksound_hub/builtin_modules/eq/lifecycle.py"
REMOTE_PATH = ROOT / "tools/soundboard_remote/ksound_soundboard_web.py"

GLASS = GLASS_PATH.read_text(encoding="utf-8")
V2 = V2_PATH.read_text(encoding="utf-8")
BASE = BASE_PATH.read_text(encoding="utf-8")
LIFECYCLE = LIFECYCLE_PATH.read_text(encoding="utf-8")
REMOTE = REMOTE_PATH.read_text(encoding="utf-8")


def _method(text: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            matches = [item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == method_name]
            assert len(matches) == 1, (class_name, method_name, len(matches))
            segment = ast.get_source_segment(text, matches[0])
            assert segment is not None
            return segment
    raise AssertionError(f"class missing: {class_name}")


def test_ip14d4_visible_build_marker_migrates_with_current_ip14_core():
    assert 'APP_BUILD = "IP' in GLASS
    assert 'Hub IP14' in REMOTE


def test_preserve_guard_intercepts_the_cleanup_the_base_constructor_really_calls():
    base_init = _method(BASE, "PipeWireAudioEngine", "__init__")
    assert "self._cleanup_stale_eq_filter_chain_processes()" in base_init
    assert "self._eq_lifecycle = EqProcessLifecycle(" in base_init

    v2_init = _method(V2, "PipeWireAudioEngine", "__init__")
    flag = v2_init.index("self._reuse_preserved_audio_runtime =")
    parent = v2_init.index("super().__init__()")
    adopt = v2_init.index("self._adopt_preserved_eq_runtimes()")
    assert flag < parent < adopt

    cleanup = _method(V2, "PipeWireAudioEngine", "_cleanup_stale_eq_filter_chain_processes")
    preserve = cleanup.index('getattr(self, "_reuse_preserved_audio_runtime", False)')
    early_return = cleanup.index("return", preserve)
    destructive = cleanup.index("PipeWireAudioEngineBase._cleanup_stale_eq_filter_chain_processes(self)")
    assert preserve < early_return < destructive


def test_cleanup_override_runtime_dispatch_preserves_then_delegates(monkeypatch):
    from ksound_hub.audio import pipewire_v2_final as v2
    calls: list[str] = []
    monkeypatch.setattr(v2.PipeWireAudioEngineBase, "_cleanup_stale_eq_filter_chain_processes", lambda _self: calls.append("base-cleanup"))
    engine = object.__new__(v2.PipeWireAudioEngine)
    engine._reuse_preserved_audio_runtime = True
    engine._cleanup_stale_eq_filter_chain_processes()
    assert calls == []
    engine._reuse_preserved_audio_runtime = False
    engine._cleanup_stale_eq_filter_chain_processes()
    assert calls == ["base-cleanup"]


def test_preserved_eq_pids_are_attached_by_module_owned_lifecycle(monkeypatch, tmp_path):
    from ksound_hub.builtin_modules.eq.lifecycle import AdoptedEqProcess, EqProcessLifecycle
    lifecycle = EqProcessLifecycle(runtime_dir=tmp_path, playback_stream_ids=lambda _key: set(), set_sink_volume=lambda _n, _v: None)
    slots = {
        "game": SimpleNamespace(proc=None, signature="old", status="old"),
        "media": SimpleNamespace(proc=None, signature="old", status="old"),
    }
    monkeypatch.setattr(lifecycle, "preserved_runtime_pids", lambda _slots, enabled: {"game": 424242} if enabled else {})
    lifecycle.adopt_preserved_runtimes(slots, enabled=True)
    handle = slots["game"].proc
    assert isinstance(handle, AdoptedEqProcess)
    assert handle.pid == 424242
    assert slots["game"].signature == ""
    assert slots["game"].status == "preserved runtime adopted"
    assert slots["media"].proc is None


def test_adopted_handle_matches_hotswap_process_contract():
    assert "class AdoptedEqProcess:" in LIFECYCLE
    handle = LIFECYCLE.split("class AdoptedEqProcess", 1)[1].split("class EqProcessLifecycle", 1)[0]
    for method_name in ("poll", "terminate", "kill", "wait"):
        assert f"def {method_name}(" in handle
    assert "self.pid" in handle
    start_slot = _method(LIFECYCLE, "EqProcessLifecycle", "start_slot")
    assert "previous_proc = slot.proc" in start_slot
    assert "new_proc = self.spawn_process(slot)" in start_slot
    assert "self.wait_for_playback_stream" in start_slot
    assert "self.terminate_process(previous_proc)" in start_slot


def test_v2_adoption_is_a_thin_bridge_to_module_lifecycle():
    preserved = _method(V2, "PipeWireAudioEngine", "_preserved_eq_runtime_pids")
    adopt = _method(V2, "PipeWireAudioEngine", "_adopt_preserved_eq_runtimes")
    assert "self._eq_lifecycle.preserved_runtime_pids(" in preserved
    assert "self._eq_lifecycle.adopt_preserved_runtimes(" in adopt


def test_full_shutdown_can_still_terminate_adopted_eq_slots():
    base_shutdown = _method(BASE, "PipeWireAudioEngine", "shutdown")
    assert "for slot in self.eq_slots.values():" in base_shutdown
    assert "self._stop_slot(slot)" in base_shutdown
    stop_slot = _method(BASE, "PipeWireAudioEngine", "_stop_slot")
    assert "self._eq_lifecycle.stop_slot(slot)" in stop_slot
    terminate = _method(LIFECYCLE, "EqProcessLifecycle", "terminate_process")
    assert "proc.poll()" in terminate
    assert "proc.pid" in terminate
    assert "proc.terminate()" in terminate
    assert "proc.wait(" in terminate
    assert "proc.kill()" in terminate
