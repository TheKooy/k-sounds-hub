"""IP15e2: older installed APK retains working detach; edge APK receives auto option."""
from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'tools/soundboard_remote/ksound_soundboard_web.py'

def test_old_and_new_android_navigation(monkeypatch,tmp_path):
    monkeypatch.setenv('KSH_CONFIG_DIR',str(tmp_path))
    (tmp_path/'soundboard.json').write_text(json.dumps({'slots':[]}))
    spec=importlib.util.spec_from_file_location('ksounds_ip15e2',WEB)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    page=mod.page().decode()
    assert 'const androidEdgeFavorites = androidRemote && apkMatch[1].startsWith("IP15e")' in page
    assert 'nativeFloating.hidden = !androidRemote || androidEdgeFavorites' in page
    assert 'document.getElementById("floatingAutoRow").hidden = !androidEdgeFavorites' in page
    node=shutil.which('node')
    if not node: return
    start=page.index('        const nativeFloating = document.getElementById("floatingFavoritesButton");')
    end=page.index('        window.addEventListener("resize", applyFavoriteLayout',start)
    js=page[start:end]
    program=''
    run=''
    script = r"""
const source = process.argv[1];
for (const [ua, expectedButton, expectedAuto] of [
  ["browser", true, true],
  ["KSHRemote/IP15d", false, true],
  ["KSHRemote/IP15e", true, false],
]) {
  const elements = {};
  const document = {getElementById(id) {
    return elements[id] ??= {hidden:true, setAttribute(n,v){this[n]=v}};
  }};
  const navigator = {userAgent:ua};
  const apkMatch = navigator.userAgent.match(/KSHRemote[/]([A-Za-z0-9][A-Za-z0-9._-]*)/);
  const FunctionRunner = new Function("document","apkMatch",source);
  FunctionRunner(document,apkMatch);
  if (elements.floatingFavoritesButton.hidden !== expectedButton || elements.floatingAutoRow.hidden !== expectedAuto)
    throw new Error("Bad floating mode for "+ua+": "+JSON.stringify(elements));
}
"""
    result=subprocess.run([node,'-e',script,js],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
