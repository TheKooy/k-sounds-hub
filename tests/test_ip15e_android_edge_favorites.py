"""IP15e edge drawer, authenticated pad artwork, automatic foreground focus and no 16 cap."""
from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
import pytest
ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'tools/soundboard_remote/ksound_soundboard_web.py'
ANDROID=ROOT/'android-soundboard-apk'

@pytest.fixture
def remote(monkeypatch,tmp_path):
    monkeypatch.setenv('KSH_CONFIG_DIR',str(tmp_path))
    (tmp_path/'soundboard.json').write_text(json.dumps({'slots':[
        {'id':f'pad-{i}','label':f'Pad {i}','path':'/tmp/s.wav'} for i in range(40)]}))
    spec=importlib.util.spec_from_file_location('ksounds_ip15e',WEB)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

def test_more_than_16_favorites_persist_and_state(remote):
    values=[{'type':'pad','id':f'pad-{i}'} for i in range(40)]
    assert remote.save_remote_favorites(values)==values
    assert remote.read_soundboard_state()['favorites']==values
    page=remote.page().decode()
    assert 'Maximum 16 favorites' not in page
    assert 'favoriteItems.length + "/16"' not in page
    assert 'floatingAutoOption' in page
    assert 'nativeFloating.hidden = !androidRemote || androidEdgeFavorites' in page

def test_edge_page_images_scroller_fx_actions_security(remote,tmp_path):
    page=remote.floating_page().decode()
    assert '/background' in page and 'meta.has_background && meta.background_version' in page
    assert '&slot=' in page and '&rev=' in page and 'encodeURIComponent(token)' in page
    assert 'overflow-y:auto' in page and 'data-filter="voice_fx"' in page
    assert '"/play"' in page and '"/voice-fx"' in page
    node=shutil.which('node')
    if node:
        js=page.split('<script>',1)[1].split('</script>',1)[0]
        target=tmp_path/'floating.js';target.write_text(js)
        outcome=subprocess.run([node,'--check',str(target)],capture_output=True,text=True)
        assert outcome.returncode==0,outcome.stderr

def test_native_edge_lifecycle_preserves_app_update_route():
    main=(ANDROID/'src/com/kooy/ksoundsoundboard/MainActivity.java').read_text()
    svc=(ANDROID/'src/com/kooy/ksoundsoundboard/FloatingFavoritesService.java').read_text()
    manifest=ET.parse(ANDROID/'AndroidManifest.xml').getroot()
    ns='{http://schemas.android.com/apk/res/android}'
    assert manifest.attrib[ns+'versionCode']=='26092203'
    for value in ('ksounds://app-options','ksounds://floating-auto-on','ksounds://floating-auto-off',
                  'PREF_EDGE_AUTO','onPageFinished','ACTION_HIDE','startFloatingFavorites(true)'):
        assert value in main
    for value in ('ACTION_SHOW','ACTION_HIDE','setOverlayVisible(false)','bubble.setVisibility(View.GONE)',
                  'favoritesParams.x = dp(29)','TYPE_APPLICATION_OVERLAY'):
        assert value in svc
