"""Public-release Remote LAN and PIN brute-force regression checks."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path


def test_remote_lan_pairing_hardening(tmp_path, monkeypatch):
    monkeypatch.setenv("KSH_CONFIG_DIR", str(tmp_path))
    root = Path(__file__).resolve().parents[1]
    script = root / "tools/soundboard_remote/ksound_soundboard_web.py"
    spec = importlib.util.spec_from_file_location("_public_remote_security_v047", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._remote_client_allowed("127.0.0.1")
    assert module._remote_client_allowed(".".join(map(str, (192, 168, 1, 20))))
    assert module._remote_client_allowed(".".join(map(str, (10, 1, 2, 3))))
    assert not module._remote_client_allowed("8.8.8.8")
    assert not module._remote_client_allowed("invalid-address")

    peer = "198.51.100.23"
    assert all(module._pairing_rate_limit_allows(peer, now=100.0) for _ in range(5))
    assert not module._pairing_rate_limit_allows(peer, now=101.0)
    assert module._pairing_rate_limit_allows(peer, now=401.0)

    module.PAIRING_PATH.write_text(
        '{"pin":"123456","expires_at":' + str(time.time() + 200) + '}',
        encoding="utf-8",
    )
    assert not module.consume_pairing_if_pin_matches("999999")
    assert module.consume_pairing_if_pin_matches("123456")
    assert not module.PAIRING_PATH.exists()
