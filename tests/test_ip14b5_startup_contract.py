from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")


def test_ip14b5_build_marker_and_preview_registry_bootstrap():
    assert 'APP_BUILD = "IP' in GLASS
    init = GLASS.split("class PreviewWindow(QMainWindow):", 1)[1].split("def _save_geometry_later", 1)[0]
    bind = "registry = self.backend_controller.module_registry"
    tune_use = 'registry.is_enabled("micro_tune")'
    vfx_use = 'registry.is_enabled("voice_fx")'
    assert bind in init
    assert tune_use in init
    assert vfx_use in init
    assert init.index(bind) < init.index(tune_use)
    assert init.index(bind) < init.index(vfx_use)


def test_core_shortcut_registration_precedes_tune_refresh():
    init = GLASS.split("class PreviewWindow(QMainWindow):", 1)[1].split("def _save_geometry_later", 1)[0]
    register = "self._register_core_shortcuts()"
    refresh = "self._refresh_micro_processing_ab_shortcut()"
    assert register in init
    assert refresh in init
    assert init.index(register) < init.index(refresh)
