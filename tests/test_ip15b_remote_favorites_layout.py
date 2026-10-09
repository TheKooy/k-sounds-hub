"""IP15b: favorites pin action on cards + persistent independent right sidebar UX."""
from __future__ import annotations
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'tools/soundboard_remote/ksound_soundboard_web.py'

@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv('KSH_CONFIG_DIR',str(tmp_path))
    (tmp_path/'soundboard.json').write_text(json.dumps({'slots':[{'id':'boom','label':'Boom','path':'/tmp/boom.wav'}]}),encoding='utf8')
    spec=importlib.util.spec_from_file_location('ksounds_remote_ip15b',WEB)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def test_pin_buttons_are_siblings_of_sound_buttons(remote):
    page=remote.page().decode('utf8')
    assert 'class="pad-wrap"' in page and 'class="favorite-pin" type="button" data-type="pad"' in page
    assert 'onclick="toggleFavorite(\'pad\', this.dataset.id)"' in page
    assert 'class="voice-fx-wrap"' in page
    assert 'data-type="voice_fx"' in page
    assert "toggleFavorite('voice_fx', this.dataset.id)" in page
    assert 'function renderFavoritePins()' in page
    assert 'event.target.closest(".favorite-pin")' in page

def test_right_rail_is_permanent_independently_scrolled(remote):
    page=remote.page().decode('utf8')
    assert 'id="favoritesRail"' in page
    assert 'id="favoritesToggle"' not in page
    assert 'position:fixed; top:14px; right:8px;' in page
    assert 'overscroll-behavior:contain;' in page
    assert 'touch-action:pan-y;' in page
    assert 'grid-template-columns:repeat(var(--favorites-columns,1)' in page
    assert 'body { padding-right: calc(var(--favorites-width, 168px) + 20px); }' in page
    assert 'favoriteRenderSignature' in page and 'list.scrollTop = scroll' in page

def test_per_device_layout_options_and_no_audio_code_changes(remote):
    page=remote.page().decode('utf8')
    for key in ('favoritesWidthOption','favoritesColumnsOption','favoritesHeightOption','padsColumnsOption',
                'ksoundFavoritesWidthV2','ksoundFavoritesColumnsV2','ksoundFavoritesTileHeightV2'):
        assert key in page
    assert 'localStorage.setItem(key,' in page
    assert 'function setFavoriteLayout(kind, value)' in page
    assert 'oninput="applyColumns(this.value, true)"' in page
    assert 'fetch("/favorites?token=' in page
    assert remote.save_remote_favorites([{'type':'pad','id':'boom'},{'type':'voice_fx','id':'robot'}]) == [
        {'type':'pad','id':'boom'},{'type':'voice_fx','id':'robot'}]

def test_embedded_js_parses_with_node_when_available(remote,tmp_path):
    node=shutil.which('node')
    if not node: pytest.skip('node unavailable')
    page=remote.page().decode('utf8')
    js=page.split('<script>',1)[1].split('</script>',1)[0]
    f=tmp_path/'remote.js';f.write_text(js,encoding='utf8')
    result=subprocess.run([node,'--check',str(f)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
