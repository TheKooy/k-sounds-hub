#!/usr/bin/env python3
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"
PIPEWIRE = ROOT / "src" / "ksound_hub" / "audio" / "pipewire.py"
V2 = ROOT / "src" / "ksound_hub" / "audio" / "pipewire_v2_final.py"

def method(text: str, cls: str, name: str) -> str:
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            matches = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(matches) != 1:
                raise AssertionError(f"{cls}.{name}: expected one method, got {len(matches)}")
            seg = ast.get_source_segment(text, matches[0])
            if seg is None:
                raise AssertionError(f"cannot extract {cls}.{name}")
            return seg
    raise AssertionError(f"class {cls} missing")

def test_meter_restart_race_contract() -> None:
    glass = GLASS.read_text(encoding="utf-8")
    base = PIPEWIRE.read_text(encoding="utf-8")
    v2 = V2.read_text(encoding="utf-8")

    shutdown = method(glass, "PreviewWindow", "_shutdown_window_runtime")
    if 'meter_timer = getattr(self, "meter_timer", None)' not in shutdown:
        raise AssertionError("Glass shutdown does not stop meter timer")
    if "meter_timer.stop()" not in shutdown:
        raise AssertionError("Glass shutdown meter timer is not stopped")
    if shutdown.find("meter_timer.stop()") > shutdown.find("backend.shutdown()"):
        raise AssertionError("meter timer must stop before backend shutdown")

    animate = method(glass, "PreviewWindow", "_animate_meters")
    if 'getattr(self, "_window_runtime_shutdown", False)' not in animate:
        raise AssertionError("late meter UI ticks are not blocked during shutdown")

    init = method(base, "PipeWireAudioEngine", "__init__")
    if "self._native_meter_shutting_down = False" not in init:
        raise AssertionError("native meter shutdown latch is not initialized")

    base_shutdown = method(base, "PipeWireAudioEngine", "shutdown")
    if "self._native_meter_shutting_down = True" not in base_shutdown:
        raise AssertionError("base shutdown does not latch native meter shutdown")
    if base_shutdown.find("self._native_meter_shutting_down = True") > base_shutdown.find("self.stop_meter_probes()"):
        raise AssertionError("native meter shutdown latch must precede meter stop")

    ensure = method(base, "PipeWireAudioEngine", "_ensure_native_meter_engine")
    if 'getattr(self, "_native_meter_shutting_down", False)' not in ensure:
        raise AssertionError("native meter can still be recreated after shutdown starts")

    v2_shutdown = method(v2, "PipeWireAudioEngine", "shutdown")
    if "self._native_meter_shutting_down = True" not in v2_shutdown:
        raise AssertionError("native playback shutdown bypasses meter shutdown latch")

if __name__ == "__main__":
    test_meter_restart_race_contract()
    print("meter restart-race contract: passed")
