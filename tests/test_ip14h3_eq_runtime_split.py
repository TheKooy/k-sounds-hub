from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPEWIRE = (ROOT / "src/ksound_hub/audio/pipewire.py").read_text(encoding="utf-8")
EQ_RUNTIME = (ROOT / "src/ksound_hub/builtin_modules/eq/runtime.py").read_text(encoding="utf-8")
EQ_MANIFEST_PATH = ROOT / "src/ksound_hub/builtin_modules/eq/module.json"
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
WEB = (ROOT / "tools/soundboard_remote/ksound_soundboard_web.py").read_text(encoding="utf-8")
V2 = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")


def test_ip14h3_is_core_only_and_eq_version_does_not_move():
    manifest = json.loads(EQ_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert 'APP_BUILD = "IP' in GLASS
    assert "Hub IP14" in WEB
    assert manifest["version"] == "1.0.0"
    assert manifest["ksounds_api"] == 1


def test_eq_runtime_definition_is_module_owned():
    manifest = json.loads(EQ_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "src/ksound_hub/builtin_modules/eq/runtime.py" in manifest["owned_paths"]
    assert "src/ksound_hub/audio/pipewire.py" in manifest["shared_paths"]
    assert "def render_eq_dropin(" in EQ_RUNTIME
    assert "def render_filters(" in EQ_RUNTIME
    assert "def stereo_width_matrix(" in EQ_RUNTIME
    assert "class EqRuntimeSlot:" in EQ_RUNTIME
    assert "def build_eq_slot_signature(" in EQ_RUNTIME
    assert "def build_eq_slot_status(" in EQ_RUNTIME


def test_core_pipewire_is_now_a_bridge_not_the_eq_filter_definition():
    assert "render_eq_dropin as _module_render_eq_dropin" in PIPEWIRE
    assert "build_eq_slot_signature as _module_build_eq_slot_signature" in PIPEWIRE
    assert "stereo_width_matrix as _module_stereo_width_matrix" in PIPEWIRE
    assert "def _stereo_width_matrix(" in PIPEWIRE
    assert "return _module_stereo_width_matrix(stereo_width)" in PIPEWIRE
    assert "def _render_filters(" not in PIPEWIRE
    assert 'filter.graph = {' not in PIPEWIRE
    assert "return _module_render_eq_dropin(" in PIPEWIRE


def test_h3_definition_split_remains_valid_after_lifecycle_extraction():
    # H3 established that filter definition/rendering is EQ-owned. Later Core
    # refactors may move lifecycle too, but must not move rendering back.
    assert "def render_eq_dropin(" in EQ_RUNTIME
    assert "def render_filters(" in EQ_RUNTIME
    assert "return _module_render_eq_dropin(" in PIPEWIRE
    assert 'filter.graph = {' not in PIPEWIRE


if __name__ == "__main__":
    test_ip14h3_is_core_only_and_eq_version_does_not_move()
    test_eq_runtime_definition_is_module_owned()
    test_core_pipewire_is_now_a_bridge_not_the_eq_filter_definition()
    test_h3_definition_split_remains_valid_after_lifecycle_extraction()
    print("IP14h3 EQ runtime split contracts: passed")
