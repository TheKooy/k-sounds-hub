from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

SERVICE_ID = "k-sounds-soundboard-remote"
RUNTIME_ROLE = "soundboard_remote_web"


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


@dataclass(frozen=True)
class RemoteRuntimeConfig:
    project_root: Path
    config_dir: Path
    ipc_socket_path: Path
    http_port: int
    discovery_port: int
    idle_timeout_seconds: int
    idle_check_seconds: int
    surfaces: tuple[str, ...] = ("pads", "voice_fx")

    @classmethod
    def from_environment(cls, project_root: Path | None = None) -> "RemoteRuntimeConfig":
        root = (project_root or Path(__file__).resolve().parents[2]).expanduser().resolve()
        config_dir = Path(
            os.environ.get(
                "KSH_CONFIG_DIR",
                str(Path.home() / ".config" / "k-sounds-hub"),
            )
        ).expanduser()
        ipc_socket = Path(
            os.environ.get(
                "KSH_IPC_SOCKET_PATH",
                f"/tmp/ksound_hub_audio_v2_{os.getuid()}.sock",
            )
        ).expanduser()
        return cls(
            project_root=root,
            config_dir=config_dir,
            ipc_socket_path=ipc_socket,
            http_port=_env_int(
                "KSOUND_SOUNDBOARD_WEB_PORT", 8765, minimum=1, maximum=65535
            ),
            discovery_port=_env_int(
                "KSOUND_SOUNDBOARD_DISCOVERY_PORT", 8766, minimum=1, maximum=65535
            ),
            idle_timeout_seconds=_env_int("KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS", 0),
            idle_check_seconds=_env_int("KSOUND_SOUNDBOARD_IDLE_CHECK_SECONDS", 30, minimum=5),
            surfaces=cls._normalize_surfaces(
                os.environ.get("KSH_REMOTE_SURFACES", "pads,voice_fx").split(",")
            ),
        )

    @staticmethod
    def _normalize_surfaces(values) -> tuple[str, ...]:
        allowed = ("pads", "voice_fx")
        requested = {str(item or "").strip().lower() for item in values}
        return tuple(item for item in allowed if item in requested)

    def with_surfaces(self, values) -> "RemoteRuntimeConfig":
        return replace(self, surfaces=self._normalize_surfaces(values))

    @property
    def runtime_dir(self) -> Path:
        return self.config_dir / "runtime"

    @property
    def web_script(self) -> Path:
        return self.project_root / "tools" / "soundboard_remote" / "ksound_soundboard_web.py"

    @property
    def log_path(self) -> Path:
        return self.runtime_dir / "soundboard-web.log"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "soundboard-web.lock"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "soundboard-web.pid"


@dataclass(frozen=True)
class RemoteLanAccessStatus:
    ready: bool
    checked: bool
    interface: str = ""
    zone: str = ""
    missing_rules: tuple[str, ...] = ()
    message: str = ""


def _run_firewall_command(
    args: list[str],
    *,
    run_factory=subprocess.run,
) -> subprocess.CompletedProcess:
    return run_factory(
        args,
        capture_output=True,
        text=True,
        timeout=1.2,
    )


def _default_route_interface(*, run_factory=subprocess.run) -> str:
    try:
        result = run_factory(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    if getattr(result, "returncode", 1) != 0:
        return ""

    for line in str(getattr(result, "stdout", "")).splitlines():
        parts = line.split()
        try:
            index = parts.index("dev")
        except ValueError:
            continue
        if index + 1 < len(parts):
            return parts[index + 1].strip()
    return ""


def remote_lan_access_status(
    config: RemoteRuntimeConfig | None = None,
    *,
    run_factory=subprocess.run,
    which_factory=shutil.which,
) -> RemoteLanAccessStatus:
    """Check whether firewalld exposes the Remote on the active LAN zone.

    The Remote health endpoint proves only that localhost can reach the web
    process.  It does not prove that Android/Web clients can cross the host
    firewall.  This probe is deliberately read-only, but some local policy
    configurations can still request authorization for firewall inspection.
    It must therefore remain an explicit diagnostic and never run in a GUI
    watchdog or another periodic background path.
    """

    runtime_config = config or RemoteRuntimeConfig.from_environment()
    if os.environ.get("KSH_REMOTE_FIREWALL_CHECK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return RemoteLanAccessStatus(
            ready=True,
            checked=False,
            message="Remote firewall check disabled",
        )

    if which_factory("firewall-cmd") is None:
        return RemoteLanAccessStatus(
            ready=True,
            checked=False,
            message="firewalld is not installed",
        )

    try:
        state = _run_firewall_command(["firewall-cmd", "--state"], run_factory=run_factory)
    except (OSError, subprocess.SubprocessError) as exc:
        return RemoteLanAccessStatus(
            ready=True,
            checked=False,
            message=f"firewalld status unavailable: {exc}",
        )

    if (
        getattr(state, "returncode", 1) != 0
        or str(getattr(state, "stdout", "")).strip() != "running"
    ):
        return RemoteLanAccessStatus(
            ready=True,
            checked=False,
            message="firewalld is not running",
        )

    interface = os.environ.get("KSH_REMOTE_INTERFACE", "").strip()
    if not interface:
        interface = _default_route_interface(run_factory=run_factory)

    zone = os.environ.get("KSH_REMOTE_FIREWALL_ZONE", "").strip()
    if not zone and interface:
        try:
            zone_result = _run_firewall_command(
                ["firewall-cmd", f"--get-zone-of-interface={interface}"],
                run_factory=run_factory,
            )
            if getattr(zone_result, "returncode", 1) == 0:
                candidate = str(getattr(zone_result, "stdout", "")).strip()
                if candidate and candidate != "no zone":
                    zone = candidate
        except (OSError, subprocess.SubprocessError):
            pass

    if not zone:
        try:
            default_result = _run_firewall_command(
                ["firewall-cmd", "--get-default-zone"],
                run_factory=run_factory,
            )
            if getattr(default_result, "returncode", 1) == 0:
                zone = str(getattr(default_result, "stdout", "")).strip()
        except (OSError, subprocess.SubprocessError):
            pass

    if not zone:
        return RemoteLanAccessStatus(
            ready=False,
            checked=True,
            interface=interface,
            message="firewalld is active but the LAN zone could not be resolved",
        )

    try:
        target_result = _run_firewall_command(
            ["firewall-cmd", f"--zone={zone}", "--get-target"],
            run_factory=run_factory,
        )
        target = str(getattr(target_result, "stdout", "")).strip().upper()
    except (OSError, subprocess.SubprocessError):
        target = ""

    if target == "ACCEPT":
        return RemoteLanAccessStatus(
            ready=True,
            checked=True,
            interface=interface,
            zone=zone,
            message=f"Remote LAN allowed by firewalld zone {zone}",
        )

    required = (
        f"{runtime_config.http_port}/tcp",
        f"{runtime_config.discovery_port}/udp",
    )
    missing: list[str] = []
    for rule in required:
        try:
            query = _run_firewall_command(
                ["firewall-cmd", f"--zone={zone}", f"--query-port={rule}"],
                run_factory=run_factory,
            )
            allowed = getattr(query, "returncode", 1) == 0 and str(
                getattr(query, "stdout", "")
            ).strip().lower() == "yes"
        except (OSError, subprocess.SubprocessError):
            allowed = False
        if not allowed:
            missing.append(rule)

    if missing:
        command = "sudo bash scripts/configure_soundboard_remote_firewall.sh"
        return RemoteLanAccessStatus(
            ready=False,
            checked=True,
            interface=interface,
            zone=zone,
            missing_rules=tuple(missing),
            message=(
                f"firewalld zone {zone} blocks Remote LAN ({', '.join(missing)}); "
                f"run {command}"
            ),
        )

    return RemoteLanAccessStatus(
        ready=True,
        checked=True,
        interface=interface,
        zone=zone,
        message=f"Remote LAN ports allowed in firewalld zone {zone}",
    )


def remote_server_health(config: RemoteRuntimeConfig) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", config.http_port, timeout=0.35)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(4096)
        if response.status != 200:
            return {}
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    finally:
        connection.close()


def remote_server_reachable(config: RemoteRuntimeConfig) -> bool:
    payload = remote_server_health(config)
    return bool(payload.get("ok")) and payload.get("service") == SERVICE_ID


def remote_server_matches_config(config: RemoteRuntimeConfig) -> bool:
    payload = remote_server_health(config)
    if not (bool(payload.get("ok")) and payload.get("service") == SERVICE_ID):
        return False
    raw = payload.get("surfaces", [])
    if not isinstance(raw, list):
        return False
    actual = RemoteRuntimeConfig._normalize_surfaces(raw)
    return actual == tuple(config.surfaces)


def _real_uid(entry: Path) -> int | None:
    try:
        text = (entry / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            return int(parts[1]) if len(parts) >= 2 else None
    return None


def _process_environment(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return result


def _command_arguments(entry: Path) -> list[str]:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return []
    return [
        chunk.decode("utf-8", errors="replace")
        for chunk in raw.split(b"\0")
        if chunk
    ]


def _has_remote_script_argument(arguments: list[str]) -> bool:
    for argument in arguments:
        path = Path(argument)
        if path.name != "ksound_soundboard_web.py":
            continue
        if "soundboard_remote" in path.parts and path.is_file():
            return True
    return False


def remote_process_pids() -> list[int]:
    proc_root = Path("/proc")
    uid = os.getuid()
    own_pid = os.getpid()
    result: list[int] = []

    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid or _real_uid(entry) != uid:
            continue
        environment = _process_environment(entry)
        arguments = _command_arguments(entry)
        if environment.get("KSH_RUNTIME_ROLE") == RUNTIME_ROLE or _has_remote_script_argument(
            arguments
        ):
            result.append(pid)
    return list(dict.fromkeys(result))


def _terminate_pids(pids: list[int], timeout: float = 2.0) -> None:
    def signal_pid(proc_pid: int) -> int:
        # In ordinary desktop sessions /proc PID and kill PID are identical.
        # Nested test sandboxes can expose a host-mounted /proc; NSpid's last
        # value is then the PID addressable from the current namespace.
        try:
            status = Path(f"/proc/{proc_pid}/status").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return proc_pid
        for line in status.splitlines():
            if line.startswith("NSpid:"):
                values = line.split()[1:]
                if values and values[-1].isdigit():
                    return int(values[-1])
        return proc_pid

    remaining = set(pids)
    for pid in remaining:
        try:
            os.kill(signal_pid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if Path(f"/proc/{pid}").exists()}
        if remaining:
            time.sleep(0.05)

    for pid in remaining:
        try:
            os.kill(signal_pid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def stop_remote_server(config: RemoteRuntimeConfig | None = None) -> int:
    runtime_config = config or RemoteRuntimeConfig.from_environment()
    pids = remote_process_pids()
    _terminate_pids(pids)
    try:
        runtime_config.pid_path.unlink()
    except FileNotFoundError:
        pass
    return len(pids)


def _spawn_remote_server(
    runtime_config: RemoteRuntimeConfig,
    *,
    python_executable: str | None = None,
) -> tuple[bool, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "KSH_RUNTIME_ROLE": RUNTIME_ROLE,
            "KSH_CONFIG_DIR": str(runtime_config.config_dir),
            "KSH_IPC_SOCKET_PATH": str(runtime_config.ipc_socket_path),
            "KSOUND_SOUNDBOARD_WEB_PORT": str(runtime_config.http_port),
            "KSOUND_SOUNDBOARD_DISCOVERY_PORT": str(runtime_config.discovery_port),
            "KSOUND_SOUNDBOARD_IDLE_TIMEOUT_SECONDS": str(
                runtime_config.idle_timeout_seconds
            ),
            "KSOUND_SOUNDBOARD_IDLE_CHECK_SECONDS": str(
                runtime_config.idle_check_seconds
            ),
            "KSH_REMOTE_SURFACES": ",".join(runtime_config.surfaces),
            "PYTHONUNBUFFERED": "1",
        }
    )

    executable = python_executable or sys.executable
    with runtime_config.log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [executable, str(runtime_config.web_script)],
            cwd=str(runtime_config.project_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    runtime_config.pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if remote_server_reachable(runtime_config):
            return True, f"Remote server ready on port {runtime_config.http_port}"
        if process.poll() is not None:
            break
        time.sleep(0.1)

    return (
        False,
        f"Remote server did not become healthy; see {runtime_config.log_path}",
    )


def ensure_remote_server(
    config: RemoteRuntimeConfig | None = None,
    *,
    python_executable: str | None = None,
) -> tuple[bool, str]:
    runtime_config = config or RemoteRuntimeConfig.from_environment()
    if not runtime_config.surfaces:
        stop_remote_server(runtime_config)
        return False, "Remote server idle: no enabled Remote surfaces"
    if not runtime_config.web_script.is_file():
        return False, f"Remote server script not found: {runtime_config.web_script}"

    runtime_config.runtime_dir.mkdir(parents=True, exist_ok=True)
    with runtime_config.lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        if remote_server_matches_config(runtime_config):
            return True, "Remote server already healthy"

        stale_pids = remote_process_pids()
        if stale_pids:
            _terminate_pids(stale_pids)

        return _spawn_remote_server(
            runtime_config,
            python_executable=python_executable,
        )


def restart_remote_server(
    config: RemoteRuntimeConfig | None = None,
    *,
    python_executable: str | None = None,
) -> tuple[bool, str]:
    """Restart the Remote atomically against the Glass watchdog.

    Holding the same runtime lock used by ensure_remote_server prevents the
    watchdog from racing between stop and start and reviving a stale script.
    """

    runtime_config = config or RemoteRuntimeConfig.from_environment()
    if not runtime_config.surfaces:
        stop_remote_server(runtime_config)
        return False, "Remote server idle: no enabled Remote surfaces"
    if not runtime_config.web_script.is_file():
        return False, f"Remote server script not found: {runtime_config.web_script}"

    runtime_config.runtime_dir.mkdir(parents=True, exist_ok=True)
    with runtime_config.lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        pids = remote_process_pids()
        if pids:
            _terminate_pids(pids)
        try:
            runtime_config.pid_path.unlink()
        except FileNotFoundError:
            pass

        return _spawn_remote_server(
            runtime_config,
            python_executable=python_executable,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the K-Sounds Soundboard Remote runtime")
    parser.add_argument("action", choices=("start", "restart", "stop", "status"))
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--check-lan",
        action="store_true",
        help="also run the explicit LAN/firewalld diagnostic for status",
    )
    args = parser.parse_args(argv)

    config = RemoteRuntimeConfig.from_environment(args.project_root)
    if args.action == "start":
        ok, message = ensure_remote_server(config)
        if not args.quiet:
            print(message)
        return 0 if ok else 1
    if args.action == "restart":
        ok, message = restart_remote_server(config)
        if not args.quiet:
            print(message)
        return 0 if ok else 1
    if args.action == "stop":
        count = stop_remote_server(config)
        if not args.quiet:
            print(f"Remote server processes stopped: {count}")
        return 0

    healthy = remote_server_reachable(config)
    if not healthy:
        if not args.quiet:
            print("stopped")
        return 1

    if args.check_lan:
        lan_status = remote_lan_access_status(config)
        if not lan_status.ready:
            if not args.quiet:
                print(f"lan-blocked: {lan_status.message}")
            return 2

    if not args.quiet:
        print("healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
