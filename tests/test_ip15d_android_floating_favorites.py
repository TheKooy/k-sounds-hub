"""IP15d: low-risk web/native overlay source contract, no Android device required."""
from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'tools/soundboard_remote/ksound_soundboard_web.py'
ANDROID = ROOT / 'android-soundboard-apk'

@pytest.fixture
def remote(monkeypatch, tmp_path):
    monkeypatch.setenv('KSH_CONFIG_DIR', str(tmp_path))
    (tmp_path / 'soundboard.json').write_text(json.dumps({'slots': [
        {'id':'pad-0', 'label':'Pad', 'path':'/tmp/p.wav'}]}))
    spec = importlib.util.spec_from_file_location('ksounds_ip15d_remote', WEB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_floating_page_only_uses_existing_favorites_and_authenticated_routes(remote):
    page = remote.floating_page().decode('utf-8')
    normal = remote.page().decode('utf-8')
    assert 'id="floatingFavoritesButton" href="ksounds://floating-toggle" hidden' in normal
    assert 'KSHRemote/IP15e' in (ANDROID/'src/com/kooy/ksoundsoundboard/MainActivity.java').read_text()
    assert 'nativeFloating.hidden = !androidRemote || androidEdgeFavorites' in normal
    assert 'ksounds://floating-close' in page
    assert 'data-filter="voice_fx"' in page
    assert 'meta.has_background && meta.background_version' in page
    assert 'Array.isArray(data.favorites)' in page
    assert '"/state"+prefix' in page
    assert '"/voice-fx"' in page and '"/play"' in page
    assert 'if parsed.path == "/floating":' in WEB.read_text()
    assert WEB.read_text().index('if not self._token_ok():') < WEB.read_text().index('if parsed.path == "/floating":')


def test_floating_js_is_parseable(remote, tmp_path):
    node = shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    page = remote.floating_page().decode()
    js = page.split('<script>',1)[1].split('</script>',1)[0]
    p = tmp_path / 'floating.js'
    p.write_text(js)
    process = subprocess.run([node,'--check',str(p)],capture_output=True,text=True)
    assert process.returncode == 0, process.stderr


def test_android_permission_foreground_overlay_and_detach_bridge():
    manifest = ET.parse(ANDROID/'AndroidManifest.xml').getroot()
    ns='{http://schemas.android.com/apk/res/android}'
    permissions = {p.attrib[ns+'name'] for p in manifest.findall('uses-permission')}
    assert 'android.permission.SYSTEM_ALERT_WINDOW' in permissions
    assert 'android.permission.FOREGROUND_SERVICE' in permissions
    service = manifest.find('application/service')
    assert service is not None
    assert service.attrib[ns+'name'] == '.FloatingFavoritesService'
    assert service.attrib[ns+'exported'] == 'false'
    activity = (ANDROID/'src/com/kooy/ksoundsoundboard/MainActivity.java').read_text()
    overlay = (ANDROID/'src/com/kooy/ksoundsoundboard/FloatingFavoritesService.java').read_text()
    for token in ('ksounds://floating-toggle','Settings.canDrawOverlays',
                  'ACTION_MANAGE_OVERLAY_PERMISSION','startForegroundService'):
        assert token in activity
    for token in ('TYPE_APPLICATION_OVERLAY','START_NOT_STICKY','startForeground(',
                  'FLAG_NOT_TOUCH_MODAL','ksounds://floating-close','getSharedPreferences("ksound_pairing"'):
        assert token in overlay
