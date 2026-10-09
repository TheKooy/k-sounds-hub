from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping

from .runtime import EqRuntimeSlot

EQ_PIPEWIRE_LATENCY = "1024/48000"  # Retained safe baseline for every EQ lane.
EQ_RETURN_MIC_CANDIDATE_LATENCY = "768/48000"  # Opt-in test, RETURN-MIC only.


def eq_latency_for_slot(key: str, env: Mapping[str, str] | None = None) -> str:
    """Resolve the filter-chain request, leaving every non-monitor lane untouched.

    The optional change is deliberately restricted to the native MICRO monitor
    experiment. It changes no PipeWire global settings, user presets or DSP.
    """
    values = os.environ if env is None else env
    if key != "return-mic":
        return EQ_PIPEWIRE_LATENCY
    native_enabled = str(values.get("KSH_MIC_OUT_NATIVE_MONITOR", "")).lower() in {
        "1", "true", "yes", "on",
    }
    if not native_enabled:
        return EQ_PIPEWIRE_LATENCY
    requested = str(values.get("KSH_RETURN_MIC_EQ_LATENCY", "")).strip()
    if requested in {"", EQ_PIPEWIRE_LATENCY}:
        return EQ_PIPEWIRE_LATENCY
    if requested == EQ_RETURN_MIC_CANDIDATE_LATENCY:
        return EQ_RETURN_MIC_CANDIDATE_LATENCY
    raise ValueError(
        "Invalid KSH_RETURN_MIC_EQ_LATENCY; supported: "
        f"{EQ_PIPEWIRE_LATENCY} or {EQ_RETURN_MIC_CANDIDATE_LATENCY}"
    )
EQ_HOT_SWAP_DUCK_FACTOR = 0.80
EQ_HOT_SWAP_READY_TIMEOUT_SECONDS = 0.09
EQ_HOT_SWAP_READY_POLL_SECONDS = 0.01


def hot_swap_duck_volume(base_volume: int) -> int:
    base = max(0, int(base_volume))
    return max(0, int(round(base * EQ_HOT_SWAP_DUCK_FACTOR)))


class AdoptedEqProcess:
    """Minimal Popen-compatible handle for a preserved EQ filter-chain."""

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)

    def poll(self):
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return None
        except OSError:
            return 0
        return None

    def terminate(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            os.kill(self.pid, signal.SIGKILL)

    def wait(self, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(["pipewire", "-c", "filter-chain.conf"], timeout)
            time.sleep(0.02)
        return 0


class EqProcessLifecycle:
    """EQ-owned PipeWire filter-chain lifecycle.

    The Core supplies only generic callbacks for stream discovery and sink
    volume. EQ owns process paths, spawn/stop, make-before-break hot-swap,
    stale cleanup, and preserved-runtime adoption.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        playback_stream_ids: Callable[[str], set[int]],
        set_sink_volume: Callable[[str, int], None],
        ladspa_paths: tuple[Path, ...] = (),
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.playback_stream_ids = playback_stream_ids
        self.set_sink_volume = set_sink_volume
        self.ladspa_paths = tuple(Path(path) for path in ladspa_paths)

    def log_path_for(self, key: str) -> Path:
        return self.runtime_dir / f"{key}-eq.log"

    def xdg_home_for(self, key: str) -> Path:
        return self.runtime_dir / f"{key}-eq-xdg"

    def dropin_path_for(self, key: str) -> Path:
        return self.xdg_home_for(key) / "pipewire" / "filter-chain.conf.d" / f"ksound-{key}-eq.conf"

    def write_slot_dropin(self, key: str, text: str) -> None:
        path = self.dropin_path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def terminate_process(self, proc) -> None:
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
        except Exception:
            pass

    def stop_slot(self, slot: EqRuntimeSlot) -> None:
        proc = slot.proc
        slot.proc = None
        slot.signature = ""
        self.terminate_process(proc)

    def cleanup_stale_filter_chain_processes(self) -> None:
        runtime_root = self.runtime_dir.resolve()
        candidates: list[int] = []
        try:
            result = subprocess.run(
                ["pgrep", "-f", r"^pipewire -c filter-chain\.conf$"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return

        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
            except Exception:
                continue
            env_path = Path(f"/proc/{pid}/environ")
            try:
                env_text = env_path.read_bytes().replace(b"\0", b"\n").decode(errors="ignore")
            except Exception:
                continue
            marker = "XDG_CONFIG_HOME="
            xdg_values = [
                item[len(marker):].strip()
                for item in env_text.splitlines()
                if item.startswith(marker)
            ]
            if not xdg_values:
                continue
            try:
                xdg_path = Path(xdg_values[-1]).resolve()
            except Exception:
                continue
            if runtime_root not in xdg_path.parents:
                continue
            if not xdg_path.name.endswith("-eq-xdg"):
                continue
            candidates.append(pid)

        if not candidates:
            return
        for signal_value in (signal.SIGTERM, signal.SIGKILL):
            for pid in candidates:
                try:
                    os.kill(pid, signal_value)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if signal_value == signal.SIGTERM:
                time.sleep(0.25)

    def spawn_process(self, slot: EqRuntimeSlot):
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(self.xdg_home_for(slot.key))
        env["LC_ALL"] = "C"
        env["PIPEWIRE_LATENCY"] = eq_latency_for_slot(slot.key, env)
        if self.ladspa_paths:
            existing = str(env.get("LADSPA_PATH", "")).strip()
            entries = [str(path) for path in self.ladspa_paths]
            if existing:
                entries.append(existing)
            env["LADSPA_PATH"] = ",".join(entries)
        with self.log_path_for(slot.key).open("ab", buffering=0) as log_file:
            return subprocess.Popen(
                ["pipewire", "-c", "filter-chain.conf"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )

    def wait_for_playback_stream(self, key: str, previous_ids: set[int], new_proc) -> bool:
        deadline = time.monotonic() + EQ_HOT_SWAP_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if new_proc.poll() is not None:
                return False
            if self.playback_stream_ids(key) - previous_ids:
                return True
            time.sleep(EQ_HOT_SWAP_READY_POLL_SECONDS)
        return new_proc.poll() is None

    def start_slot(self, slot: EqRuntimeSlot, signature: str, *, handover_volume: int = 0) -> None:
        if slot.proc is not None and slot.signature == signature and slot.proc.poll() is None:
            return

        previous_proc = slot.proc if slot.proc is not None and slot.proc.poll() is None else None
        use_hot_swap = previous_proc is not None and int(handover_volume) > 0
        if not use_hot_swap:
            self.stop_slot(slot)
            slot.proc = self.spawn_process(slot)
            slot.signature = signature
            return

        previous_stream_ids = self.playback_stream_ids(slot.key)
        duck_volume = hot_swap_duck_volume(handover_volume)
        self.set_sink_volume(slot.logical_sink, duck_volume)

        new_proc = None
        try:
            # Make before break: keep the audible old filter until the new
            # filter-chain has connected to its playback target.
            new_proc = self.spawn_process(slot)
            if not self.wait_for_playback_stream(slot.key, previous_stream_ids, new_proc):
                raise RuntimeError(f"replacement EQ filter-chain exited for {slot.key}")
            slot.proc = new_proc
            slot.signature = signature
            self.terminate_process(previous_proc)
            new_proc = None
        except Exception:
            # The previous slot remains attached on failure.
            self.terminate_process(new_proc)
            raise
        finally:
            # Restore the exact configured volume on success and failure.
            self.set_sink_volume(slot.logical_sink, handover_volume)

    def read_log_tail(self, key: str) -> str:
        log_path = self.log_path_for(key)
        if not log_path.is_file():
            return ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return ""
        return lines[-1].strip() if lines else ""

    def preserved_runtime_pids(self, slots: Mapping[str, EqRuntimeSlot], *, enabled: bool) -> dict[str, int]:
        if not enabled:
            return {}
        runtime_root = self.runtime_dir.resolve()
        by_key: dict[str, list[tuple[int, int]]] = {}
        proc_root = Path("/proc")
        uid = os.getuid()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                status_text = (entry / "status").read_text(encoding="utf-8", errors="replace")
                real_uid = None
                for line in status_text.splitlines():
                    if line.startswith("Uid:"):
                        parts = line.split()
                        real_uid = int(parts[1]) if len(parts) >= 2 else None
                        break
                if real_uid != uid:
                    continue
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
                if "pipewire -c filter-chain.conf" not in cmdline:
                    continue
                env_text = (entry / "environ").read_bytes().replace(b"\0", b"\n").decode("utf-8", errors="replace")
                xdg_value = ""
                for line in env_text.splitlines():
                    if line.startswith("XDG_CONFIG_HOME="):
                        xdg_value = line.split("=", 1)[1].strip()
                if not xdg_value:
                    continue
                xdg_path = Path(xdg_value).resolve()
                if runtime_root not in xdg_path.parents or not xdg_path.name.endswith("-eq-xdg"):
                    continue
                key = xdg_path.name[:-len("-eq-xdg")]
                if key not in slots:
                    continue
                stat_fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").split()
                start_ticks = int(stat_fields[21]) if len(stat_fields) > 21 else pid
                by_key.setdefault(key, []).append((start_ticks, pid))
            except Exception:
                continue

        adopted: dict[str, int] = {}
        for key, candidates in by_key.items():
            candidates.sort()
            _start, keep_pid = candidates[-1]
            adopted[key] = keep_pid
            for _old_start, old_pid in candidates[:-1]:
                try:
                    AdoptedEqProcess(old_pid).terminate()
                except Exception:
                    pass
        return adopted

    def adopt_preserved_runtimes(self, slots: Mapping[str, EqRuntimeSlot], *, enabled: bool) -> None:
        for key, pid in self.preserved_runtime_pids(slots, enabled=enabled).items():
            slot = slots.get(key)
            if slot is None:
                continue
            slot.proc = AdoptedEqProcess(pid)
            slot.signature = ""
            slot.status = "preserved runtime adopted"
