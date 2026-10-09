from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = ROOT / "tools" / "soundboard_remote" / "ksound_soundboard_web.py"
ANDROID_SOURCE = ROOT / "android-soundboard-apk" / "src" / "com" / "kooy" / "ksoundsoundboard" / "MainActivity.java"
ANDROID_MANIFEST = ROOT / "android-soundboard-apk" / "AndroidManifest.xml"
AUDIT_COLLECTOR = ROOT / "tools" / "collect-soundboard-remote-audit.sh"


def _load_web_module(config_dir: Path):
    previous = os.environ.get("KSH_CONFIG_DIR")
    os.environ["KSH_CONFIG_DIR"] = str(config_dir)
    try:
        name = f"ksound_soundboard_web_test_{id(config_dir)}"
        spec = importlib.util.spec_from_file_location(name, WEB_SOURCE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("KSH_CONFIG_DIR", None)
        else:
            os.environ["KSH_CONFIG_DIR"] = previous


def _write_soundboard(config_dir: Path, image_path: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "global_volume": 77,
        "folders": ["Memes", "Alerts"],
        "slots": [
            {
                "id": "sb1",
                "label": "Airhorn",
                "path": "/sounds/airhorn.wav",
                "folder": "Memes",
                "background_path": str(image_path),
            },
            {
                "id": "sb2",
                "label": "Warning",
                "path": "/sounds/warning.wav",
                "folder": "Alerts",
            },
        ],
    }
    (config_dir / "soundboard.json").write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def _expect_not_modified(url: str, etag: str) -> None:
    request = urllib.request.Request(url, headers={"If-None-Match": etag})
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 304
    else:
        raise AssertionError("expected HTTP 304")


def test_remote_state_and_background_use_device_cache(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    image_path = tmp_path / "cover.png"
    image_path.write_bytes(b"not-decoded-by-server")
    _write_soundboard(config_dir, image_path)
    module = _load_web_module(config_dir)

    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    token = urllib.parse.quote(module.TOKEN, safe="")
    try:
        state_url = f"http://127.0.0.1:{port}/state?token={token}"
        with urllib.request.urlopen(state_url, timeout=3) as response:
            state_etag = response.headers["ETag"]
            assert "no-cache" in response.headers["Cache-Control"]
            state = json.loads(response.read())
        assert state["slots"][0]["auto_colors"] == list(module.automatic_pad_colors("sb1"))
        _expect_not_modified(state_url, state_etag)

        version = urllib.parse.quote(state["slots"][0]["background_version"], safe="")
        background_url = (
            f"http://127.0.0.1:{port}/background?token={token}&slot=sb1&rev={version}"
        )
        with urllib.request.urlopen(background_url, timeout=3) as response:
            background_etag = response.headers["ETag"]
            assert "immutable" in response.headers["Cache-Control"]
            assert response.read() == b"not-decoded-by-server"
        _expect_not_modified(background_url, background_etag)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_remote_page_reconciles_in_memory_without_full_reload(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    image_path = tmp_path / "cover.png"
    image_path.write_bytes(b"cover")
    _write_soundboard(config_dir, image_path)
    module = _load_web_module(config_dir)
    page = module.page().decode("utf-8")

    assert "function reconcileSoundboardState(data)" in page
    assert "reconcileSoundboardState(data);" in page
    assert "window.location.replace" not in page
    assert 'cache: "no-cache"' in page
    assert "IntersectionObserver" in page
    assert 'window.localStorage.getItem("ksoundSoundboardFolderV2")' in page
    assert "folder-style-drawer" in page
    assert '>Side drawer</option>' in page


def test_remote_identity_and_android_keep_local_cache() -> None:
    web = WEB_SOURCE.read_text(encoding="utf-8")
    android = ANDROID_SOURCE.read_text(encoding="utf-8")
    manifest = ANDROID_MANIFEST.read_text(encoding="utf-8")
    collector = AUDIT_COLLECTOR.read_text(encoding="utf-8")

    assert 'SERVICE_NAME = "K-Sounds Soundboard Remote"' in web
    assert "K-Sounds Soundboard Remote" in manifest
    assert "WebSettings.LOAD_DEFAULT" in android
    assert "WebSettings.LOAD_NO_CACHE" not in android
    assert "clearCache(true)" not in android
    assert "WebStorage.getInstance().deleteAllData()" not in android
    assert "setOffscreenPreRaster(true)" in android
    assert "git ls-files --cached --others --exclude-standard" in collector
    assert "src/ksound_hub/glass_app.py" in collector


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ksh-remote-cache-test-") as raw:
        root = Path(raw)
        test_remote_state_and_background_use_device_cache(root / "cache")
        test_remote_page_reconciles_in_memory_without_full_reload(root / "page")
    test_remote_identity_and_android_keep_local_cache()
    print("Soundboard Remote in-memory/cache contracts: passed")


def test_remote_grid_supports_extra_zoom_and_optional_play_mode_pinch(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "soundboard.json").write_text(
        json.dumps({
            "remote_columns": 8,
            "remote_pinch_outside_edit": False,
            "slots": [{"id": "sb1", "label": "One", "path": ""}],
        }) + "\n",
        encoding="utf-8",
    )
    module = _load_web_module(config_dir)

    settings = module.read_soundboard_settings()
    assert settings["remote_columns"] == 8
    assert settings["remote_pinch_outside_edit"] is False

    module.update_soundboard_setting("remote_columns", 13)
    module.update_soundboard_setting("remote_pinch_outside_edit", "true")
    settings = module.read_soundboard_settings()
    assert settings["remote_columns"] == 13
    assert settings["remote_pinch_outside_edit"] is True

    # Values above the UI range stay bounded and the option remains a real boolean.
    module.update_soundboard_setting("remote_columns", 99)
    module.update_soundboard_setting("remote_pinch_outside_edit", "false")
    settings = module.read_soundboard_settings()
    assert settings["remote_columns"] == 13
    assert settings["remote_pinch_outside_edit"] is False

    page = module.page().decode("utf-8")
    assert 'id="pinchOutsideEditOption"' in page
    assert 'Remote IP10t' in page
    assert '"remote_build": "IP10t"' in Path(module.__file__).read_text(encoding="utf-8")
    assert "GRID_MAX_COLUMNS = 13" in page
    assert "Math.min(GRID_MAX_COLUMNS, n)" in page
    assert "PINCH_RATIO_PER_STEP = 1.08" in page
    assert "pinchState.startDistance / distance" in page
    assert 'function pinchAllowed()' in page
    assert 'const EDIT_HINT = "Edit: tap to select · Hold and drag to insert · Pinch to resize"' in page
    assert 'const PLAY_HINT = "Tap to play · Hold and drag onto a pad to swap"' in page
    assert 'inserting:editMode' in page
    assert 'const marker = document.createComment("swap")' in page
    assert '.pad-wrap.swap-target .pad' in page
    # Pads always use JS-owned gestures so Android native pan-y cannot cancel a held drag.
    assert 'touch-action: none;' in page
