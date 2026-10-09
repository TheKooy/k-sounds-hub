from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLASS = ROOT / "src" / "ksound_hub" / "glass_app.py"
RUNTIME = ROOT / "src" / "ksound_hub" / "soundboard_remote_runtime.py"
MANIFEST = ROOT / "android-soundboard-apk" / "AndroidManifest.xml"
ANDROID = (
    ROOT
    / "android-soundboard-apk"
    / "src"
    / "com"
    / "kooy"
    / "ksoundsoundboard"
    / "MainActivity.java"
)
FIREWALL = ROOT / "scripts" / "configure_soundboard_remote_firewall.sh"


def _class_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            source = ast.get_source_segment(text, node)
            assert source is not None
            return source
    raise AssertionError(f"class missing: {name}")


def test_firewall_helper_uses_the_interface_zone_and_durable_rules() -> None:
    source = FIREWALL.read_text(encoding="utf-8")
    assert "--get-zone-of-interface" in source
    assert "--get-default-zone" in source
    assert 'RULES=("${HTTP_PORT}/tcp" "${DISCOVERY_PORT}/udp")' in source
    assert '--permanent --add-port="$rule"' in source
    assert '--permanent --query-port="$rule"' in source
    assert "kooy-home" not in source
    assert subprocess.run(
        ["bash", "-n", str(FIREWALL)],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0


def test_firewall_helper_check_mode_detects_non_persistent_rules(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_ip = fake_bin / "ip"
    fake_ip.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'default via 198.51.100.1 dev enp9s0 proto dhcp'\n",
        encoding="utf-8",
    )
    fake_ip.chmod(0o755)

    fake_firewall = fake_bin / "firewall-cmd"
    fake_firewall.write_text(
        """#!/usr/bin/env bash
args=" $* "
if [[ "$args" == *" --permanent "* && "$args" == *" --query-port=8765/tcp "* ]]; then
  echo no
  exit 1
fi
case "$args" in
  *" --state "*) echo running ;;
  *" --get-zone-of-interface=enp9s0 "*) echo kooy-home ;;
  *" --get-target "*) echo default ;;
  *" --query-port=8765/tcp "*) echo yes ;;
  *" --query-port=8766/udp "*) echo no; exit 1 ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_firewall.chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    result = subprocess.run(
        ["bash", str(FIREWALL), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "zone kooy-home" in result.stdout
    assert "8765/tcp (permanent)" in result.stdout
    assert "8766/udp (runtime permanent)" in result.stdout


def test_firewall_checks_are_explicit_and_never_polled_by_glass() -> None:
    glass = GLASS.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    assert "Remote: Server online" in glass
    assert "remote_lan_access_status" not in glass
    assert '"--check-lan"' in runtime
    assert "if args.check_lan:" in runtime
    assert 'print(f"lan-blocked: {lan_status.message}")' in runtime
    assert "return 2" in runtime


def test_android_discovery_has_retries_directed_broadcast_and_manual_fallback() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    java = ANDROID.read_text(encoding="utf-8")
    assert "android.permission.ACCESS_WIFI_STATE" in manifest
    assert "android.permission.CHANGE_WIFI_MULTICAST_STATE" in manifest
    assert "DhcpInfo" in java
    assert "createMulticastLock" in java
    assert "for (int attempt = 0; attempt < 3; attempt++)" in java
    assert 'serverInput.setHint("PC local IP address or host name")' in java
    assert 'makeButton("Use address"' in java
    assert 'new URL(baseUrl + "/health")' in java
    assert '"k-sounds-soundboard-remote".equals' in java


def test_sound_pad_has_visible_press_feedback_before_playback() -> None:
    source = _class_source(GLASS, "SoundPadCard")
    assert "self._pressed_feedback_timer.setInterval(145)" in source
    assert "self._set_pressed_feedback(True)" in source
    assert "layout.setContentsMargins(7, 7, 3, 3)" in source
    assert "painter.fillPath(path, QColor(42, 190, 225, 64))" in source
    assert "QTimer.singleShot(0, lambda: callback(slot_key))" in source
