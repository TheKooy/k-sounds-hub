"""IP15c Remote: full-height right rail, compact controls, proportional pin stars."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "tools/soundboard_remote/ksound_soundboard_web.py"


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv("KSH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "soundboard.json").write_text(json.dumps({"slots": [
        {"id": "a", "label": "Alpha", "path": "/tmp/a.wav"},
        {"id": "b", "label": "Bravo", "path": "/tmp/b.wav"},
    ]}), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("ksounds_remote_ip15c", WEB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rail_reaches_bottom_and_bar_yields_right_column(remote):
    page = remote.page().decode("utf-8")
    assert "bottom:calc(8px + env(safe-area-inset-bottom, 0px))" in page
    assert "bottom:calc(105px + env(safe-area-inset-bottom, 0px))" not in page
    assert "right: calc(var(--favorites-width, 168px) + 20px);" in page
    assert ".selection-bar { position: fixed; z-index: 45; left: 14px;" in page
    assert "grid-template-columns: 46px 46px minmax(0, 1fr) 46px" in page
    assert "grid-template-columns:38px 38px minmax(0,1fr) 38px" in page
    assert "overflow-y:auto; overflow-x:hidden; min-height:0; flex:1;" in page
    assert "overscroll-behavior:contain;" in page


def test_pin_is_proportional_and_not_oversized_on_mobile(remote):
    page = remote.page().decode("utf-8")
    assert ".pad-wrap, .voice-fx-wrap { container-type:inline-size; }" in page
    assert "width:clamp(19px,14cqw,25px); height:clamp(19px,14cqw,25px)" in page
    assert "font-size:clamp(12px,9cqw,16px)" in page
    assert ".favorite-pin { width:28px; height:28px;" not in page
    assert "padding-right:29px" in page
    assert "Favorites IP15c" in page
    assert "onclick=\"toggleFavorite('pad', this.dataset.id)\"" in page


def test_embedded_javascript_syntax(remote, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    page = remote.page().decode("utf-8")
    js = page.split("<script>", 1)[1].split("</script>", 1)[0]
    output = tmp_path / "remote.js"
    output.write_text(js, encoding="utf-8")
    run = subprocess.run([node, "--check", str(output)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
