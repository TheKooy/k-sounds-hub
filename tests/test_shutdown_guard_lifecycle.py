#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"
PIPEWIRE = ROOT / "src" / "ksound_hub" / "audio" / "pipewire.py"
V2 = ROOT / "src" / "ksound_hub" / "audio" / "pipewire_v2_final.py"


def _class_method(text: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            matches = [item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == method_name]
            if len(matches) != 1:
                raise AssertionError(f"expected one {class_name}.{method_name}, got {len(matches)}")
            segment = ast.get_source_segment(text, matches[0])
            if segment is None:
                raise AssertionError(f"cannot read {class_name}.{method_name}")
            return segment
    raise AssertionError(f"class {class_name} not found")


def test_shutdown_guard_lifecycle_contract() -> None:
    glass = GLASS.read_text(encoding="utf-8")
    base = PIPEWIRE.read_text(encoding="utf-8")
    v2 = V2.read_text(encoding="utf-8")

    shutdown_window = _class_method(glass, "PreviewWindow", "_shutdown_window_runtime")
    if 'getattr(self, "_window_runtime_shutdown", False)' not in shutdown_window:
        raise AssertionError("Glass shutdown is not guarded/idempotent")
    if 'self._window_runtime_shutdown = True' not in shutdown_window:
        raise AssertionError("Glass shutdown guard is never latched")
    if shutdown_window.find('self._window_runtime_shutdown = True') > shutdown_window.find('backend.shutdown()'):
        raise AssertionError("shutdown guard must latch before backend teardown")

    if 'app.aboutToQuit.connect(self._shutdown_window_runtime)' not in glass:
        raise AssertionError("QApplication.aboutToQuit does not own final K-Sounds shutdown")

    base_shutdown = _class_method(base, "PipeWireAudioEngine", "shutdown")
    meter_at = base_shutdown.find("self.stop_meter_probes()")
    slot_at = base_shutdown.find("for slot in self.eq_slots.values()")
    if meter_at < 0 or slot_at < 0 or meter_at > slot_at:
        raise AssertionError("base shutdown must stop native meters before EQ teardown")
    if "self._cleanup_stale_meter_parec_processes()" not in base_shutdown:
        raise AssertionError("base shutdown lacks final stale-meter cleanup")

    v2_shutdown = _class_method(v2, "PipeWireAudioEngine", "shutdown")
    if "self.stop_meter_probes()" not in v2_shutdown:
        raise AssertionError("native playback shutdown bypasses native meter stop")
    if "for probe in self._meter_probes.values():" in v2_shutdown:
        raise AssertionError("native playback shutdown still bypasses stop_meter_probes")


if __name__ == "__main__":
    test_shutdown_guard_lifecycle_contract()
    print("shutdown guard + meter lifecycle contract: passed")
