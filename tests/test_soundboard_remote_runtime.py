from __future__ import annotations

import subprocess
from pathlib import Path

from ksound_hub.soundboard_remote_runtime import (
    RemoteLanAccessStatus,
    RemoteRuntimeConfig,
    _has_remote_script_argument,
    main,
    remote_lan_access_status,
)


def test_remote_runtime_config_uses_canonical_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "KSH_CONFIG_DIR",
        "KSH_IPC_SOCKET_PATH",
        "KSOUND_SOUNDBOARD_WEB_PORT",
        "KSOUND_SOUNDBOARD_DISCOVERY_PORT",
        "KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS",
        "KSOUND_SOUNDBOARD_IDLE_CHECK_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    config = RemoteRuntimeConfig.from_environment(tmp_path / "repo")

    assert config.config_dir == tmp_path / ".config" / "k-sounds-hub"
    assert config.http_port == 8765
    assert config.discovery_port == 8766
    assert config.idle_timeout_seconds == 0
    assert config.idle_check_seconds == 30
    assert config.web_script == (
        tmp_path / "repo" / "tools" / "soundboard_remote" / "ksound_soundboard_web.py"
    )


def test_remote_runtime_config_preserves_dynamic_environment(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    socket_path = tmp_path / "runtime.sock"
    monkeypatch.setenv("KSH_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KSH_IPC_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("KSOUND_SOUNDBOARD_WEB_PORT", "18765")
    monkeypatch.setenv("KSOUND_SOUNDBOARD_DISCOVERY_PORT", "18766")
    monkeypatch.setenv("KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("KSOUND_SOUNDBOARD_IDLE_CHECK_SECONDS", "10")

    config = RemoteRuntimeConfig.from_environment(tmp_path / "repo")

    assert config.config_dir == config_dir
    assert config.ipc_socket_path == socket_path
    assert config.http_port == 18765
    assert config.discovery_port == 18766
    assert config.idle_timeout_seconds == 900
    assert config.idle_check_seconds == 10


def test_remote_process_match_requires_a_real_script_argument(tmp_path: Path) -> None:
    script = tmp_path / "tools" / "soundboard_remote" / "ksound_soundboard_web.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert _has_remote_script_argument(
        ["python3", str(script)]
    )
    assert not _has_remote_script_argument(
        ["bash", "-lc", "pgrep -af tools/soundboard_remote/ksound_soundboard_web.py"]
    )
    assert not _has_remote_script_argument(["rg", "ksound_soundboard_web.py"])


def _firewall_runner(*, http_allowed: bool, discovery_allowed: bool):
    responses = {
        ("firewall-cmd", "--state"): (0, "running\n"),
        ("ip", "-4", "route", "show", "default"): (
            0,
            "default via 198.51.100.1 dev enp9s0 proto dhcp\n",
        ),
        ("firewall-cmd", "--get-zone-of-interface=enp9s0"): (0, "test-lan\n"),
        ("firewall-cmd", "--zone=test-lan", "--get-target"): (0, "default\n"),
        ("firewall-cmd", "--zone=test-lan", "--query-port=8765/tcp"): (
            0 if http_allowed else 1,
            "yes\n" if http_allowed else "no\n",
        ),
        ("firewall-cmd", "--zone=test-lan", "--query-port=8766/udp"): (
            0 if discovery_allowed else 1,
            "yes\n" if discovery_allowed else "no\n",
        ),
    }

    def run(args, *, capture_output, text, timeout):
        assert capture_output is True
        assert text is True
        assert timeout == 1.2
        returncode, stdout = responses[tuple(args)]
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    return run


def _test_config(tmp_path: Path) -> RemoteRuntimeConfig:
    return RemoteRuntimeConfig(
        project_root=tmp_path / "repo",
        config_dir=tmp_path / "config",
        ipc_socket_path=tmp_path / "runtime.sock",
        http_port=8765,
        discovery_port=8766,
        idle_timeout_seconds=0,
        idle_check_seconds=30,
    )


def test_remote_lan_status_reports_the_actual_interface_zone(tmp_path: Path) -> None:
    config = _test_config(tmp_path)
    status = remote_lan_access_status(
        config,
        run_factory=_firewall_runner(http_allowed=False, discovery_allowed=False),
        which_factory=lambda _name: "/usr/bin/firewall-cmd",
    )

    assert status.checked is True
    assert status.ready is False
    assert status.interface == "enp9s0"
    assert status.zone == "test-lan"
    assert status.missing_rules == ("8765/tcp", "8766/udp")
    assert "configure_soundboard_remote_firewall.sh" in status.message


def test_remote_lan_status_is_ready_when_both_rules_are_allowed(tmp_path: Path) -> None:
    config = _test_config(tmp_path)
    status = remote_lan_access_status(
        config,
        run_factory=_firewall_runner(http_allowed=True, discovery_allowed=True),
        which_factory=lambda _name: "/usr/bin/firewall-cmd",
    )

    assert status.checked is True
    assert status.ready is True
    assert status.zone == "test-lan"
    assert status.missing_rules == ()


def test_remote_lan_status_does_not_require_firewalld(tmp_path: Path) -> None:
    config = _test_config(tmp_path)
    status = remote_lan_access_status(
        config,
        run_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runner must not be called")
        ),
        which_factory=lambda _name: None,
    )

    assert status.checked is False
    assert status.ready is True
    assert status.message == "firewalld is not installed"


def test_status_default_never_checks_firewall(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(
        "ksound_hub.soundboard_remote_runtime.remote_server_reachable",
        lambda _config: True,
    )
    monkeypatch.setattr(
        "ksound_hub.soundboard_remote_runtime.remote_lan_access_status",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("default status must not query firewalld")
        ),
    )

    assert main(["status", "--project-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "healthy\n"


def test_status_lan_check_is_opt_in(monkeypatch, tmp_path: Path, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        "ksound_hub.soundboard_remote_runtime.remote_server_reachable",
        lambda _config: True,
    )

    def blocked(config: RemoteRuntimeConfig) -> RemoteLanAccessStatus:
        calls.append(config)
        return RemoteLanAccessStatus(
            checked=True,
            ready=False,
            message="missing 8765/tcp",
            interface="enp9s0",
            zone="test-lan",
            missing_rules=("8765/tcp",),
        )

    monkeypatch.setattr(
        "ksound_hub.soundboard_remote_runtime.remote_lan_access_status",
        blocked,
    )

    assert main(["status", "--project-root", str(tmp_path), "--check-lan"]) == 2
    assert len(calls) == 1
    assert capsys.readouterr().out == "lan-blocked: missing 8765/tcp\n"


def test_restart_action_uses_atomic_restart(monkeypatch, tmp_path: Path, capsys) -> None:
    calls = []

    def restart(config: RemoteRuntimeConfig):
        calls.append(config.project_root)
        return True, "Remote server ready on port 8765"

    monkeypatch.setattr(
        "ksound_hub.soundboard_remote_runtime.restart_remote_server",
        restart,
    )

    assert main(["restart", "--project-root", str(tmp_path)]) == 0
    assert calls == [tmp_path.resolve()]
    assert capsys.readouterr().out == "Remote server ready on port 8765\n"
