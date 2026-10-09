"""IP15a: mixed-pad/FX favorites persist on the PC without touching audio state."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "tools/soundboard_remote/ksound_soundboard_web.py"


@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv("KSH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "soundboard.json").write_text(
        json.dumps({"slots": [{"id": "boom", "label": "Boom", "path": "/tmp/boom.wav"}]}),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("remote_favorites_ip15a", WEB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_favorites_persist_and_live_state_is_not_stale(remote):
    assert remote.read_soundboard_state()["favorites"] == []
    expected = [{"type": "pad", "id": "boom"}, {"type": "voice_fx", "id": "robot"}]
    assert remote.save_remote_favorites(expected) == expected
    assert remote.read_soundboard_state()["favorites"] == expected
    assert json.loads(remote.FAVORITES_PATH.read_text())["items"] == expected
    assert stat.S_IMODE(remote.FAVORITES_PATH.stat().st_mode) == 0o600
    assert remote.read_voice_fx_state()["active"] == []
    assert json.loads(remote.CONFIG_PATH.read_text())["slots"][0]["id"] == "boom"
    assert remote.save_remote_favorites([]) == []
    assert remote.read_soundboard_state()["favorites"] == []


def test_favorites_validate_id_and_duplicates(remote):
    assert remote.normalize_remote_favorites([
        {"type": "voice_fx", "id": "ROBOT"},
        {"type": "voice_fx", "id": "robot"},
        {"type": "unsupported", "id": "robot"},
    ]) == [{"type": "voice_fx", "id": "robot"}]
    with pytest.raises(ValueError, match="Unknown pad"):
        remote.save_remote_favorites([{"type": "pad", "id": "deleted"}])
    assert not remote.FAVORITES_PATH.exists()


def test_favorites_are_token_protected_and_available_through_state(remote):
    server = ThreadingHTTPServer(("127.0.0.1", 0), remote.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    items = [{"type": "pad", "id": "boom"}, {"type": "voice_fx", "id": "robot"}]
    try:
        body = urlencode({"items": json.dumps(items)}).encode()
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(url + "/favorites?token=invalid", data=body), timeout=3)
        assert denied.value.code == 403
        with urlopen(Request(url + "/favorites?token=" + remote.TOKEN, data=body), timeout=3) as response:
            assert json.load(response)["favorites"] == items
        with urlopen(url + "/state?token=" + remote.TOKEN, timeout=3) as response:
            assert json.load(response)["favorites"] == items
        with pytest.raises(HTTPError) as invalid:
            urlopen(Request(url + "/favorites?token=" + remote.TOKEN,
                            data=urlencode({"items": json.dumps([{"type": "pad", "id": "wrong"}])}).encode()), timeout=3)
        assert invalid.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_favorite_rail_and_editor_exist_in_real_page(remote):
    page = remote.page().decode("utf-8")
    for token in ('id="favoritesRail"', 'id="favoriteEditor"',
                  'function activateFavorite(item)', 'function renderFavoriteEditor()',
                  'function reconcileFavorites(state)', 'fetch("/favorites?token='):
        assert token in page
