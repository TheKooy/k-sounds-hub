"""IP15e1: update distribution contract, foreground notification badge suppression."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
ANDROID=ROOT/'android-soundboard-apk'

def test_overlay_foreground_notification_has_own_non_badge_channel():
    src=(ANDROID/'src/com/kooy/ksoundsoundboard/FloatingFavoritesService.java').read_text()
    assert '"ksounds_floating_favorites_no_badge"' in src
    assert 'channel.setShowBadge(false);' in src
    assert 'startForeground(NOTIFICATION_ID, notification())' in src
    assert '.setOngoing(true)' in src
    assert 'ACTION_STOP' in src

def test_version_is_newer_than_ip15d_and_ip15e():
    manifest=ET.parse(ANDROID/'AndroidManifest.xml').getroot()
    ns='{http://schemas.android.com/apk/res/android}'
    assert int(manifest.attrib[ns+'versionCode'])==26092203
    assert manifest.attrib[ns+'versionName']=='0.4.9-edge-dev2'
