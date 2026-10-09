from __future__ import annotations


import json
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..models import AppSettings, ChannelConfig, EqProfile
from ..builtin_modules.eq.lifecycle import AdoptedEqProcess as _AdoptedEqProcess
from .engine import AudioNode
from .pipewire import (
    CONTROL_NODE_BY_CHANNEL,
    PLAYBACK_EQ_CHANNELS,
    STATUS_LABELS,
    STATUS_ORDER,
    _build_eq_slot_signature,
    PipeWireAudioEngine as PipeWireAudioEngineBase,
)

PLAYBACK_KEYS = tuple(PLAYBACK_EQ_CHANNELS.keys())
NATIVE_PLAYBACK_FAILURE_LIMIT = 3
NATIVE_PLAYBACK_FAILURE_WINDOW_SECONDS = 15.0

MIC_PHYSICAL_SOURCE_BY_LABEL: dict[str, str] = {}

RETURN_MIC_MONITOR_SOURCE_PREFIX = "source:"
RETURN_MIC_MONITOR_STATIC_SOURCE_BY_KEY = {
    "soundboard": "soundboard.monitor",
    "micro-final": "micro",
}
RETURN_MIC_MONITOR_MEDIA_PREFIX = "KSH-MICOUT-Monitor-"


class PipeWireAudioEngine(PipeWireAudioEngineBase):
    """
    V2 native final-render playback engine.

    Playback keeps the visible Pulse/PipeWire channel sinks, but the actual
    final mix/render is delegated to the native C++ engine. Python only writes
    the channel state file and reads the levels file back for meters.

    Micro / return-mic intentionally stay on the inherited implementation.
    """

    def _native_playback_requested(self) -> bool:
        return str(os.environ.get("KSH_NATIVE_PLAYBACK", "0")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    def _native_playback_enabled(self) -> bool:
        # Emergency safe default: native final-render playback is disabled.
        # It can be re-enabled explicitly with KSH_NATIVE_PLAYBACK=1 after the
        # pacat/native playback instability is fixed.
        return self._native_playback_requested() and not bool(
            getattr(self, "_v2_fallback_reason", "")
        )

    def _cleanup_stale_eq_filter_chain_processes(self) -> None:
        """Preserve live EQ chains during a UI/Core handoff.

        PipeWireAudioEngineBase.__init__() calls this method before eq_slots are
        created.  IP14d3 guarded a different compatibility hook, so the base
        constructor still killed the live GAME/CHAT/MEDIA/MORE filter-chains.
        Dynamic dispatch lands here first; preserve mode must return without
        touching them, while an ordinary cold start keeps the historical stale
        runtime cleanup.
        """
        if getattr(self, "_reuse_preserved_audio_runtime", False):
            return
        PipeWireAudioEngineBase._cleanup_stale_eq_filter_chain_processes(self)

    def _preserved_eq_runtime_pids(self) -> dict[str, int]:
        return self._eq_lifecycle.preserved_runtime_pids(
            self.eq_slots,
            enabled=bool(getattr(self, "_reuse_preserved_audio_runtime", False)),
        )

    def _adopt_preserved_eq_runtimes(self) -> None:
        self._eq_lifecycle.adopt_preserved_runtimes(
            self.eq_slots,
            enabled=bool(getattr(self, "_reuse_preserved_audio_runtime", False)),
        )

    def __init__(self) -> None:
        self._v2_fallback_reason = ""
        self._v2_engine_failures: list[float] = []
        self._last_playback_settings: AppSettings | None = None
        # This flag must exist before the base constructor runs: legacy/base
        # startup hooks may consult our cleanup overrides during initialization.
        self._reuse_preserved_audio_runtime = str(os.environ.get("KSH_PRESERVE_AUDIO_ROUTING", "0")).strip().lower() in {"1", "true", "yes", "on"}
        super().__init__()
        self._adopt_preserved_eq_runtimes()
        self._native_runtime_dir = self.runtime_dir / "native-engine"
        self._native_runtime_dir.mkdir(parents=True, exist_ok=True)
        self._v2_state_path = self._native_runtime_dir / "render_state.txt"
        self._v2_volume_state_path = self._native_runtime_dir / "volume_state.txt"
        self._v2_levels_path = self._native_runtime_dir / "levels.json"
        self._v2_engine_log = self._native_runtime_dir / "native-engine.log"
        self._v2_engine_proc: subprocess.Popen | None = None
        self._native_micro_state_path = self._native_runtime_dir / "micro_state.txt"
        self._native_micro_log = self._native_runtime_dir / "native-micro-engine.log"
        self._native_micro_levels_path = self._native_runtime_dir / "micro_levels.json"
        self._native_micro_proc: subprocess.Popen | None = None
        self._native_micro_external_pid: int | None = None
        self._native_micro_rnnoise_available_cache: bool | None = None
        self._last_micro_state_signature = ""
        self._state_io_lock = threading.RLock()
        self._return_mic_legacy_cleanup_done = False
        self._return_mic_final_muted = True
        self._return_mic_final_playback_ids_seen: tuple[str, ...] = ()
        self._return_mic_expected_sources: dict[str, str] = {}
        self._native_mic_out_route_allowed = False
        self._native_mic_out_native_sources: dict[str, str] = {}
        self._native_micro_levels_cache_mtime_ns = 0
        self._native_micro_levels_cache_payload: dict[str, Any] = {}
        self._last_state_signature = ""
        self._last_volume_state_signature = ""
        self._v2_levels_cache_mtime_ns = 0
        self._v2_levels_cache_payload: dict[str, Any] = {}
        if self._native_playback_enabled() and not self._native_engine_binary().is_file():
            self._activate_v2_fallback("native playback binary missing")

        if self._native_playback_enabled():
            self._disable_legacy_playback_slots()
        else:
            for slot in self.eq_slots.values():
                slot.status = "legacy playback safe mode"

    def _disable_legacy_playback_slots(self) -> None:
        for slot in self.eq_slots.values():
            self._stop_slot(slot)
            slot.status = "v2 native final-render"
        self._kill_legacy_python_render()

    def _kill_legacy_filter_chains(self) -> None:
        # Kept as a compatibility hook, but cleanup must remain restricted to
        # filter-chain processes whose XDG_CONFIG_HOME belongs to K-Sounds.
        # During a UI-only handoff those chains are the live routing session,
        # so treating them as stale would defeat the whole preserve operation.
        if self._reuse_preserved_audio_runtime:
            return
        self._cleanup_stale_eq_filter_chain_processes()

    def _kill_legacy_python_render(self) -> None:
        for pattern in (
            r"pacat --playback .*--client-name=K-Sounds",
            r"pw-cat --playback .*--media-name=K-Sounds",
        ):
            try:
                proc = self._run(["pgrep", "-af", pattern])
            except Exception:
                continue
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                parts = line.split(None, 1)
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if pid == os.getpid():
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass

    def shutdown(self) -> None:
        self._native_meter_shutting_down = True
        if not self._native_playback_enabled():
            self._stop_v2_engine()
            self._stop_native_micro_engine()
            PipeWireAudioEngineBase.shutdown(self)
            return

        self._stop_v2_engine()
        self._stop_native_micro_engine()
        self.stop_meter_probes()
        for key in list(self._micro_links):
            self._unload_micro_link(key)
        self._disable_return_mic()

    def status_text(self) -> str:
        if not self._native_playback_enabled():
            suffix = "playback-v2: disabled safe legacy"
            if self._v2_fallback_reason:
                suffix = f"playback-v2: automatic safe fallback ({self._v2_fallback_reason})"
            return PipeWireAudioEngineBase.status_text(self) + f" • {suffix}"

        parts = [self._status_base(), "playback-v2: native final-render"]
        for key in STATUS_ORDER:
            slot = self.eq_slots[key]
            parts.append(f"{STATUS_LABELS[key]}: {slot.status}")
        return " • ".join(parts)

    def _current_profile(self, channel: ChannelConfig) -> EqProfile:
        wanted = channel.selected_eq_profile
        for profile in channel.eq_profiles:
            if profile.name == wanted:
                return profile
        return channel.eq_profiles[0]

    def _bands_spec(self, profile: EqProfile) -> str:
        bands = []
        for band in profile.bands:
            bands.append(f"{float(band.frequency):.3f}:{float(band.gain_db):.3f}:{float(band.q):.3f}")
        return ",".join(bands)

    def _native_render_key_for_channel(self, key: str) -> str:
        # UI/settings key is return-mic, but the real PipeWire sink/source is retour.
        # The native playback engine derives capture source as "<key>.monitor",
        # so return-mic must be rendered as "retour".
        return "retour" if key == "return-mic" else key

    def _render_channel_line(self, channel: ChannelConfig) -> str:
        render_key = self._native_render_key_for_channel(channel.key)
        # Fast path: channel.primary_target is already the real PipeWire sink
        # name selected by the UI. Do not call _resolve_playback_target() here
        # on every slider tick, because that can run pactl repeatedly.
        target_sink = (channel.primary_target or "").strip()
        if target_sink:
            target_label = target_sink
        else:
            target = self._resolve_playback_target(channel)
            target_label = target.label if target is not None else "system default"
            target_sink = target.sink_name if target is not None else ""

        profile = self._current_profile(channel)
        stereo_width = max(0, min(100, int(getattr(channel, "stereo_width", 100))))
        fields = [
            "channel",
            render_key,
            "1" if channel.enabled else "0",
            "1" if channel.muted else "0",
            str(int(channel.volume)),
            target_label,
            target_sink,
            self._bands_spec(profile),
            str(stereo_width),
        ]
        return "\t".join(fields)

    def _render_state_text(self, settings: AppSettings) -> str:
        lines = ["version\t2"]
        for key in PLAYBACK_KEYS:
            channel = self._find_channel(settings, key)
            if channel is None:
                continue
            lines.append(self._render_channel_line(channel))
        return "\n".join(lines) + "\n"

    def _write_v2_state(self, settings: AppSettings) -> None:
        with self._state_io_lock:
            self._last_playback_settings = settings
            text = self._render_state_text(settings)
            signature = text
            if signature == self._last_state_signature and self._v2_state_path.exists():
                return
            self._last_state_signature = signature
            tmp = self._v2_state_path.with_suffix(".txt.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._v2_state_path)

    def _render_volume_state_text(self, settings: AppSettings) -> str:
        lines = ["version\t1"]
        for key in PLAYBACK_KEYS:
            channel = self._find_channel(settings, key)
            if channel is None:
                continue
            render_key = self._native_render_key_for_channel(channel.key)
            lines.append(
                f"volume\t{render_key}\t{'1' if channel.muted else '0'}\t{int(channel.volume)}"
            )
        return "\n".join(lines) + "\n"


    def _write_v2_volume_state(self, settings: AppSettings) -> None:
        with self._state_io_lock:
            self._last_playback_settings = settings
            text = self._render_volume_state_text(settings)
            if text == self._last_volume_state_signature and self._v2_volume_state_path.exists():
                return

            self._last_volume_state_signature = text
            self._v2_volume_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._v2_volume_state_path.with_suffix(".txt.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._v2_volume_state_path)

    def _kill_orphan_native_micro_runtime(self) -> None:
        """Remove native micro engines that outlived the Python owner.

        This intentionally targets only the native micro engine binary and the
        exact long-running parec/pacat children it creates. It does not change
        routing, EQ slots, soundboard settings, or PipeWire modules.
        """

        patterns = (
            "ksound_native_micro_engine",
            "parec --device=soundboard.monitor --raw --format=float32le --rate=48000 --channels=2 --latency-msec=20",
            "pacat --playback --device=micro_bus --raw --format=float32le --rate=48000 --channels=2 --latency-msec=40 --process-time-msec=10",
        )

        pids: list[int] = []
        for pattern in patterns:
            try:
                proc = self._run(["pgrep", "-u", str(os.getuid()), "-af", pattern])
            except Exception:
                continue
            if proc.returncode != 0:
                continue

            for line in proc.stdout.splitlines():
                parts = line.split(None, 1)
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if pid == os.getpid() or pid in pids:
                    continue
                pids.append(pid)

        if not pids:
            return

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in list(pids):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if sig == signal.SIGTERM:
                time.sleep(0.3)

    def _native_micro_enabled(self) -> bool:
        return str(os.environ.get("KSH_NATIVE_MIC", "1")).strip().lower() not in {"0", "false", "no", "off"}

    def _native_monitor_requested(self) -> bool:
        """Experimental IP14n9a switch; absent means exact IP14n8 routing."""
        return str(os.environ.get("KSH_MIC_OUT_NATIVE_MONITOR", "0")).strip().lower() in {
            "1", "true", "yes", "on",
        }

    def _native_monitor_eligible(self, channel: ChannelConfig) -> bool:
        """Only replace the MICRO source, never another MIC OUT input."""
        if not self._native_monitor_requested() or not self._native_micro_enabled():
            return False
        if not self._native_micro_engine_binary().is_file():
            return False
        linked = {str(key).strip().lower() for key in (channel.linked_channels or [])}
        return bool(linked & {"micro-final", "micro", "source:micro"})

    def _preserved_native_micro_pid(self) -> int | None:
        if not self._reuse_preserved_audio_runtime:
            return None

        cached = self._native_micro_external_pid
        if cached is not None:
            try:
                os.kill(int(cached), 0)
                return int(cached)
            except (ProcessLookupError, PermissionError, OSError):
                self._native_micro_external_pid = None

        binary = str(self._native_micro_engine_binary())
        state_path = str(self._native_micro_state_path)
        proc_root = Path("/proc")
        uid = os.getuid()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                status = (entry / "status").read_text(encoding="utf-8", errors="replace")
                real_uid = None
                for line in status.splitlines():
                    if line.startswith("Uid:"):
                        parts = line.split()
                        real_uid = int(parts[1]) if len(parts) >= 2 else None
                        break
                if real_uid != uid:
                    continue
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
            except Exception:
                continue
            if binary in cmdline and "--state" in cmdline and state_path in cmdline:
                self._native_micro_external_pid = pid
                return pid
        return None

    def _native_micro_transport_running(self) -> bool:
        proc = self._native_micro_proc
        if proc is not None and proc.poll() is None:
            return True
        return self._preserved_native_micro_pid() is not None

    def _apply_micro_endpoint_controls(self, channel: ChannelConfig) -> None:
        """Keep the exported MICRO endpoint stable while native DSP owns gain.

        Changing the Pulse/PipeWire volume of the remapped ``micro`` source can
        make an already connected WebRTC capture sound doubled/comb-filtered.
        The native transport now owns the user gain, so its exported endpoint
        stays unmuted at unity. If the native child is unavailable, preserve
        the legacy endpoint-volume fallback exactly as before.
        """
        if self._native_micro_enabled() and self._native_micro_transport_running():
            self._run_no_fail(["pactl", "set-source-volume", "micro", "100%"])
            self._run_no_fail(["pactl", "set-source-mute", "micro", "0"])
            return

        PipeWireAudioEngineBase._apply_micro_endpoint_controls(self, channel)

    def _native_micro_engine_binary(self) -> Path:
        return Path(__file__).resolve().parents[3] / "native_engine" / "build" / "ksound_native_micro_engine"

    def native_micro_rnnoise_available(self) -> bool:
        cached = self._native_micro_rnnoise_available_cache
        if cached is not None:
            return bool(cached)
        engine_bin = self._native_micro_engine_binary()
        if not engine_bin.is_file():
            self._native_micro_rnnoise_available_cache = False
            return False
        try:
            probe = subprocess.run(
                [str(engine_bin), "--probe-rnnoise"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=0.8, check=False,
            )
            available = probe.returncode == 0 and "rnnoise=available" in probe.stdout
        except Exception:
            available = False
        self._native_micro_rnnoise_available_cache = bool(available)
        return bool(available)

    def _audio_source_names(self) -> list[str]:
        proc = self._run(["pactl", "list", "short", "sources"])
        if proc.returncode != 0:
            return []
        names: list[str] = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                names.append(parts[1])
        return names

    def _micro_physical_source_for_label(self, label: str) -> str:
        wanted = (label or "").strip()

        if wanted:
            if self._is_valid_micro_input_source_name(wanted) and self._source_exists(wanted):
                return wanted
            return ""

        return self._default_micro_source_name()

    def _native_micro_source_for_channel(self, channel: ChannelConfig) -> str:
        wanted = (channel.primary_target or "").strip()

        # Explicit selection is authoritative. Missing hardware means silence,
        # not an implicit switch to another microphone.
        if wanted:
            if self._is_valid_micro_input_source_name(wanted) and self._source_exists(wanted):
                return wanted
            return ""

        names = self._resolved_micro_source_names(channel)
        if names:
            return names[0]

        return self._resolved_micro_source_name(channel)



    def _ensure_micro_endpoint(self) -> None:
        if not self._sink_exists("micro_bus"):
            self._run_no_fail([
                "pactl",
                "load-module",
                "module-null-sink",
                "sink_name=micro_bus",
                "sink_properties=device.description=🎤MICRO-BUS",
            ])

        if not self._source_exists("micro"):
            self._run_no_fail([
                "pactl",
                "load-module",
                "module-remap-source",
                "master=micro_bus.monitor",
                "source_name=micro",
                "source_properties=device.description=🎤MICRO",
            ])

        self._run_no_fail(["pactl", "set-sink-mute", "micro_bus", "0"])
        self._run_no_fail(["pactl", "set-sink-volume", "micro_bus", "100%"])
        self._run_no_fail(["pactl", "set-source-mute", "micro", "0"])
        self._run_no_fail(["pactl", "set-source-volume", "micro", "100%"])
        self._run_no_fail(["pactl", "set-default-source", "micro"])

    def _cleanup_legacy_micro_loopbacks_for_native(self) -> None:
        proc = self._run(["pactl", "list", "short", "modules"])
        if proc.returncode != 0:
            return

        needles = (
            "K-Sound Hub Mic Physical",
            "K-Sound Hub Mic Send",
            "K-Sound-Hub-Soundboard-To-Micro",
        )

        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue

            module_id, module_name, args = parts
            if module_name != "module-loopback":
                continue

            if any(needle in args for needle in needles):
                self._run_no_fail(["pactl", "unload-module", module_id])

    def _channel_send_gain_for_micro(self, settings: AppSettings, channel_key: str) -> float:
        channel = self._find_channel(settings, channel_key)
        if channel is None or not channel.enabled or channel.muted:
            return 0.0

        try:
            volume = int(channel.volume)
        except Exception:
            volume = 100

        volume = max(0, min(150, volume))
        return max(0.0, min(1.5, float(volume) / 100.0))

    def _apply_micro_link_send_volumes(self, settings: AppSettings) -> None:
        micro_channel = self._find_channel(settings, "micro")
        if micro_channel is None:
            linked: set[str] = set()
            micro_muted = True
        else:
            linked = {str(key).lower() for key in getattr(micro_channel, "linked_channels", []) or []}
            micro_muted = bool(micro_channel.muted) or not bool(micro_channel.enabled)

        for key in ("all", "game", "chat", "media", "more"):
            media_name = self._micro_send_media_name(key)
            gain = self._channel_send_gain_for_micro(settings, key) if key in linked else 0.0
            volume = max(0, min(150, int(round(gain * 100.0))))
            muted = "1" if micro_muted or key not in linked or gain <= 0.0 else "0"

            for sink_input_id in self._find_sink_input_ids_by_media_name(media_name):
                self._run_no_fail(["pactl", "set-sink-input-volume", sink_input_id, f"{volume}%"])
                self._run_no_fail(["pactl", "set-sink-input-mute", sink_input_id, muted])

    def _unload_legacy_ksh_mic_physical_loopbacks(self) -> None:
        """Native MICRO engine owns physical mic -> micro_bus. Remove old Pulse loopback."""
        proc = self._run(["pactl", "list", "short", "modules"])
        if proc.returncode != 0:
            return

        for line in proc.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue

            module_id, module_name, args = parts
            lowered = args.lower()

            if module_name != "module-loopback":
                continue
            if "sink=micro_bus" not in lowered:
                continue
            if "ksh_mic_physical" not in lowered:
                continue

            self._run_no_fail(["pactl", "unload-module", module_id])

    def _native_micro_soundboard_send_to_micro(self) -> bool:
        try:
            configured_path = getattr(self, "_soundboard_config_path", None)
            soundboard_path = (
                Path(configured_path)
                if configured_path
                else Path.home() / ".config" / "k-sounds-hub" / "soundboard.json"
            )

            if not soundboard_path.is_file():
                return False

            soundboard_data = json.loads(soundboard_path.read_text(encoding="utf-8"))
            if not isinstance(soundboard_data, dict):
                return False

            raw_send_to_micro = soundboard_data.get("send_to_micro")
            if isinstance(raw_send_to_micro, bool):
                return raw_send_to_micro

            slots = soundboard_data.get("slots", [])
            if isinstance(slots, list):
                return any(
                    bool(slot.get("send_to_micro"))
                    for slot in slots
                    if isinstance(slot, dict)
                )
        except Exception:
            return False

        return False

    def _render_native_micro_state_text(self, settings: AppSettings) -> str:
        channel = self._find_channel(settings, "micro")
        if channel is None:
            return (
                "version\t10\nenabled\t0\nmuted\t1\nvolume\t0\nsource\t\n"
                "processing_enabled\t0\nnoise_suppression_enabled\t0\n"
                "gate_enabled\t0\ngate_threshold\t35\neq_bands\t\n"
                "limiter_ceiling_db\t-1.00\nvoice_fx_enabled\t0\n"
                "voice_fx_preset\tradio\nvoice_fx_active\t\nvoice_fx_amount\t72\n"
                "custom_fx_enabled\t0\ncustom_fx_wet\t100\n"
            )

        source_name = self._native_micro_source_for_channel(channel)
        linked = {str(key).lower() for key in getattr(channel, "linked_channels", []) or []}

        try:
            micro_volume = int(channel.volume)
        except Exception:
            micro_volume = 100
        micro_volume = max(0, min(150, micro_volume))

        allowed_micro_injection = {"all", "game", "chat", "media", "more"}
        raw_micro_injection = getattr(settings, "glass_micro_injection_channels", []) or []
        if not isinstance(raw_micro_injection, list):
            raw_micro_injection = []
        micro_injection = {
            str(key).strip().lower()
            for key in raw_micro_injection
            if str(key).strip().lower() in allowed_micro_injection
        }

        try:
            injection_volume = int(getattr(settings, "glass_micro_injection_volume", 125))
        except Exception:
            injection_volume = 125
        injection_gain = max(0.0, min(2.0, injection_volume / 100.0))

        send_gains: dict[str, float] = {}
        for key in ("all", "game", "chat", "media", "more"):
            if key in linked:
                send_gains[key] = max(send_gains.get(key, 0.0), self._channel_send_gain_for_micro(settings, key))
            if key in micro_injection:
                send_gains[key] = max(send_gains.get(key, 0.0), injection_gain)

        monitor_channel = self._find_channel(settings, "return-mic")
        # MIC OUT's headphone gain/mute belongs to its FINAL renderer (or the
        # muted retour bus in native playback), not to the source transport.
        # Keep the native feed armed while the user mutes/unmutes the headset;
        # a temporary safety mute during route changes must never latch the
        # C++ engine in native_monitor=0 after the renderer is restored.
        native_monitor = bool(
            getattr(self, "_native_mic_out_route_allowed", False)
            and monitor_channel is not None
            and monitor_channel.enabled
            and self._native_monitor_eligible(monitor_channel)
        )

        # The same native MICRO engine can monitor a physical input without a
        # PipeWire module-loopback. This is exclusive of the processed MICRO
        # tap: the input selector exposes exactly one of these destinations.
        native_return_sources = (
            dict(getattr(self, "_native_mic_out_native_sources", {}) or {})
            if not native_monitor and monitor_channel is not None
            and monitor_channel.enabled and getattr(self, "_native_mic_out_route_allowed", False)
            else {}
        )
        lines = [
            "version\t10",
            f"native_monitor\t{1 if native_monitor else 0}",
            f"return_enabled\t{1 if native_return_sources else 0}",
            "return_muted\t0",  # headphone mute belongs to the final renderer
            "return_volume\t100",  # final renderer alone owns volume
            "return_target\tretour",
            f"enabled\t{'1' if channel.enabled else '0'}",
            f"muted\t{'1' if channel.muted else '0'}",
            f"volume\t{micro_volume}",
            f"source\t{source_name}",
            f"hard_gate_enabled\t{1 if bool(getattr(channel, 'hard_gate_enabled', False)) else 0}",
            f"hard_gate_threshold\t{max(0, min(100, int(getattr(channel, 'hard_gate_threshold', 12))))}",
            f"processing_enabled\t{1 if bool(getattr(channel, 'micro_processing_enabled', False)) else 0}",
            f"input_gain_db\t{max(-12.0, min(24.0, float(getattr(channel, 'micro_input_gain_db', 0.0)))):.2f}",
            f"noise_suppression_enabled\t{1 if bool(getattr(channel, 'micro_noise_suppression_enabled', False)) else 0}",
            f"noise_suppression\t{max(0, min(100, int(getattr(channel, 'micro_noise_suppression', 0))))}",
            f"gate_enabled\t{1 if bool(getattr(channel, 'micro_gate_enabled', False)) else 0}",
            f"gate_threshold\t{max(0, min(100, int(getattr(channel, 'micro_gate_threshold', 35))))}",
            f"eq_bands\t{self._bands_spec(self._current_profile(channel))}",
            f"compressor_enabled\t{1 if bool(getattr(channel, 'micro_compressor_enabled', False)) else 0}",
            f"compressor_amount\t{max(0, min(100, int(getattr(channel, 'micro_compressor_amount', 45))))}",
            f"deesser_enabled\t{1 if bool(getattr(channel, 'micro_deesser_enabled', False)) else 0}",
            f"deesser_amount\t{max(0, min(100, int(getattr(channel, 'micro_deesser_amount', 35))))}",
            f"limiter_enabled\t{1 if bool(getattr(channel, 'micro_limiter_enabled', True)) else 0}",
            f"limiter_ceiling_db\t{max(-12.0, min(0.0, float(getattr(channel, 'micro_limiter_ceiling_db', -1.0)))):.2f}",
            f"voice_fx_enabled\t{1 if bool(getattr(channel, 'micro_voice_fx_active', [])) else 0}",
            f"voice_fx_preset\t{str((getattr(channel, 'micro_voice_fx_active', []) or [getattr(channel, 'micro_voice_fx_preset', 'radio')])[0] or 'radio').strip().lower()}",
            "voice_fx_active\t" + ",".join(
                key for key in ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
                if key in {str(item or "").strip().lower() for item in (getattr(channel, 'micro_voice_fx_active', []) or [])}
            ),
            f"voice_fx_amount\t{max(0, min(100, int(getattr(channel, 'micro_voice_fx_amount', 72))))}",
        ]

        custom_preset = getattr(channel, "micro_custom_fx_preset", {})
        if not isinstance(custom_preset, dict):
            custom_preset = {}
        try:
            custom_wet = max(0.0, min(100.0, float(custom_preset.get("wet_percent", 100.0))))
        except (TypeError, ValueError):
            custom_wet = 100.0
        custom_modules = custom_preset.get("modules", [])
        if not isinstance(custom_modules, list):
            custom_modules = []
        lines.append(f"custom_fx_enabled\t{1 if bool(getattr(channel, 'micro_custom_fx_enabled', False)) and bool(custom_modules) else 0}")
        lines.append(f"custom_fx_wet\t{custom_wet:.2f}")
        for raw_module in custom_modules[:16]:
            if not isinstance(raw_module, dict) or not bool(raw_module.get("enabled", True)):
                continue
            module_type = str(raw_module.get("type", "") or "").strip().lower()
            params = raw_module.get("params", {})
            if not isinstance(params, dict):
                params = {}
            try:
                if module_type == "gain":
                    value = max(-24.0, min(24.0, float(params.get("gain_db", 0.0))))
                    lines.append(f"custom_fx_module\tgain\t{value:.4f}\t0\t0")
                elif module_type in {"highpass", "lowpass"}:
                    value = max(40.0, min(18000.0, float(params.get("frequency_hz", 120.0 if module_type == 'highpass' else 7000.0))))
                    lines.append(f"custom_fx_module\t{module_type}\t{value:.4f}\t0\t0")
                elif module_type == "saturation":
                    value = max(1.0, min(10.0, float(params.get("drive", 1.8))))
                    lines.append(f"custom_fx_module\tsaturation\t{value:.4f}\t0\t0")
                elif module_type == "delay":
                    delay_ms = max(10.0, min(500.0, float(params.get("delay_ms", 120.0))))
                    feedback = max(0.0, min(85.0, float(params.get("feedback_percent", 25.0))))
                    mix = max(0.0, min(100.0, float(params.get("mix_percent", 30.0))))
                    lines.append(f"custom_fx_module\tdelay\t{delay_ms:.4f}\t{feedback:.4f}\t{mix:.4f}")
                elif module_type == "distortion":
                    drive = max(1.0, min(20.0, float(params.get("drive", 6.0))))
                    mix = max(0.0, min(100.0, float(params.get("mix_percent", 75.0))))
                    lines.append(f"custom_fx_module\tdistortion\t{drive:.4f}\t{mix:.4f}\t0")
                elif module_type == "pitch":
                    semitones = max(-12.0, min(12.0, float(params.get("semitones", -5.0))))
                    mix = max(0.0, min(100.0, float(params.get("mix_percent", 100.0))))
                    lines.append(f"custom_fx_module\tpitch\t{semitones:.4f}\t{mix:.4f}\t0")
                elif module_type == "reverb":
                    predelay = max(0.0, min(100.0, float(params.get("predelay_ms", 22.0))))
                    decay = max(0.0, min(100.0, float(params.get("decay_percent", 50.0))))
                    mix = max(0.0, min(100.0, float(params.get("mix_percent", 35.0))))
                    lines.append(f"custom_fx_module\treverb\t{predelay:.4f}\t{decay:.4f}\t{mix:.4f}")
            except (TypeError, ValueError):
                continue

        for key in ("all", "game", "chat", "media", "more"):
            if key in send_gains:
                gain = max(0.0, min(2.0, float(send_gains.get(key, 0.0))))
                enabled = "1" if gain > 0.0 else "0"
                lines.append(f"send\t{key}\t{enabled}\t{key}.monitor\t{gain:.4f}")

        for key, source in sorted(native_return_sources.items()):
            # Device names come from validated PipeWire source discovery.
            # Never write state-file separators from a device-provided label.
            key = str(key).replace("\t", "").replace("\n", "")
            source = str(source).replace("\t", "").replace("\n", "")
            lines.append(f"return_source\t{key}\t1\t{source}\t1.0000")

        soundboard_to_micro = "soundboard" in linked or self._native_micro_soundboard_send_to_micro()

        if soundboard_to_micro:
            lines.append("send\tsoundboard\t1\tsoundboard.monitor\t1.0")

        return "\n".join(lines) + "\n"

    def _write_native_micro_state(self, settings: AppSettings) -> None:
        with self._state_io_lock:
            text = self._render_native_micro_state_text(settings)
            if text == self._last_micro_state_signature and self._native_micro_state_path.exists():
                return

            self._last_micro_state_signature = text
            self._native_micro_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._native_micro_state_path.with_suffix(".txt.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._native_micro_state_path)

    def refresh_native_micro_state(self, settings: AppSettings) -> None:
        """Refresh the native MICRO engine state without changing routes."""
        if not self._native_micro_enabled():
            return
        self._write_native_micro_state(settings)
        self._unload_legacy_ksh_mic_physical_loopbacks()
        self._ensure_native_micro_engine()
        self._unload_legacy_ksh_mic_physical_loopbacks()

    def _ensure_native_micro_engine(self) -> bool:
        engine_bin = self._native_micro_engine_binary()
        if not engine_bin.is_file():
            return False

        # During a Glass/Core handoff, keep the already-running native MICRO
        # process instead of killing/recreating it merely because this new
        # Python object does not own the original Popen handle.
        if self._native_micro_transport_running():
            return True

        self._stop_native_micro_engine()

        env = os.environ.copy()
        env["KSH_RUNTIME_ROLE"] = "native_micro_engine"
        self._native_micro_log.parent.mkdir(parents=True, exist_ok=True)

        with self._native_micro_log.open("ab", buffering=0) as log_file:
            self._native_micro_proc = subprocess.Popen(
                [
                    str(engine_bin),
                    "--state",
                    str(self._native_micro_state_path),
                    "--log",
                    str(self._native_micro_log),
                    "--levels",
                    str(self._native_micro_levels_path),
                    "--period-ms",
                    "10",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
            )

        return self._native_micro_proc is not None and self._native_micro_proc.poll() is None

    def _stop_native_micro_engine(self) -> None:
        proc = self._native_micro_proc
        external_pid = self._native_micro_external_pid
        self._native_micro_proc = None
        self._native_micro_external_pid = None

        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                pass

        if external_pid is not None:
            try:
                os.kill(int(external_pid), signal.SIGTERM)
                time.sleep(0.08)
            except Exception:
                pass

        self._kill_orphan_native_micro_runtime()

    def reconcile_selected_micro_source_presence(self, settings: AppSettings) -> None:
        # Fail closed when the explicitly selected physical MICRO disappears.
        # Called by a low-frequency Qt watchdog, never by the native DSP loop
        # and never by the high-frequency meter timer.
        channel = self._find_channel(settings, "micro")
        if channel is None:
            return

        wanted = (channel.primary_target or "").strip()
        if not wanted:
            return

        present = bool(
            self._is_valid_micro_input_source_name(wanted)
            and self._source_exists(wanted)
        )
        state = (wanted, present)

        if getattr(self, "_selected_micro_presence_state", None) == state:
            return

        self._selected_micro_presence_state = state

        # _native_micro_source_for_channel() now returns "" while the selected
        # source is absent. The native engine hot-reloads that state and its
        # existing empty-source path produces digital silence.
        if self._native_micro_enabled():
            self._write_native_micro_state(settings)
            return

        # Keep the legacy path on the same fail-closed contract.
        self._apply_micro_transport(settings)

    def _apply_micro_transport(self, settings: AppSettings) -> None:
        if self._native_micro_enabled():
            try:
                self._ensure_micro_endpoint()
                self._write_native_micro_state(settings)

                self._unload_legacy_ksh_mic_physical_loopbacks()
                if self._ensure_native_micro_engine():
                    self._cleanup_legacy_micro_loopbacks_for_native()
                    self._run_no_fail(["pactl", "set-default-source", "micro"])
                    channel = self._find_channel(settings, "micro")
                    if channel is not None:
                        self._apply_micro_endpoint_controls(channel)
                    return
            except Exception:
                self._stop_native_micro_engine()

        # Fallback legacy path.
        # Important: when KSH_NATIVE_MIC=0, the legacy path still needs the
        # final virtual microphone endpoint. Without this, the fallback can
        # disable the native micro engine but leave no working "micro" source.
        self._ensure_micro_endpoint()

        channel = self._find_channel(settings, "micro")
        if channel is not None:
            self._ensure_physical_micro_loopbacks(channel)
            self._run_no_fail(["pactl", "set-default-source", "micro"])

        self._apply_micro_links(settings)
        self._apply_micro_link_send_volumes(settings)
        if channel is not None:
            self._apply_micro_endpoint_controls(channel)

    def _apply_native_micro_channel(self, settings: AppSettings) -> None:
        """Apply MICRO without ever recreating the legacy physical loopback.

        Native playback is intentionally disabled by default, but native MICRO
        is enabled independently. The old safe-mode dispatch used the base
        implementation for every MICRO source/volume change. That created a
        second ``KSH_MIC_PHYSICAL`` loopback beside the native engine, producing
        doubled/comb-filtered audio until a Gate refresh removed it again.
        """
        channel = self._find_channel(settings, "micro")
        if channel is None:
            return

        for node_name in self._resolved_micro_source_names(channel):
            # The physical/EasyEffects source stays at unity. The MICRO slider
            # controls the exported virtual source through endpoint controls.
            self._run_no_fail(["pactl", "set-source-volume", node_name, "100%"])
            self._run_no_fail(["pactl", "set-source-mute", node_name, "0"])

        self._apply_micro_transport(settings)
        self._apply_micro_endpoint_controls(channel)
        self._apply_return_mic(settings)


    def _return_monitor_media_name(self, key: str) -> str:
        return RETURN_MIC_MONITOR_MEDIA_PREFIX + str(key)

    def _return_monitor_module_records(self) -> dict[str, list[tuple[str, str, str]]]:
        """Discover only MIC OUT-owned loopbacks, including their real endpoints.

        A media-name match by itself is insufficient: a module surviving a
        crash may keep the same key while still capturing a previous source.
        """
        proc = self._run(["pactl", "list", "short", "modules"])
        if proc.returncode != 0:
            return {}

        found: dict[str, list[tuple[str, str, str]]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            module_id, module_name, args = parts
            if module_name != "module-loopback" or not module_id.isdigit():
                continue
            marker = "sink_input_properties=media.name=" + RETURN_MIC_MONITOR_MEDIA_PREFIX
            media = next((token[len(marker):] for token in args.split() if token.startswith(marker)), "")
            if not media:
                continue
            source = next((token[7:] for token in args.split() if token.startswith("source=")), "")
            sink = next((token[5:] for token in args.split() if token.startswith("sink=")), "")
            found.setdefault(media, []).append((module_id, source, sink))
        return found

    def _return_monitor_existing_module_ids(self) -> dict[str, list[str]]:
        return {
            key: [module_id for module_id, _source, _sink in records]
            for key, records in self._return_monitor_module_records().items()
        }

    def _cleanup_return_monitor_sources(self, keep_keys: set[str] | None = None) -> None:
        keep_keys = keep_keys or set()
        expected = getattr(self, "_return_mic_expected_sources", {})
        for key, records in self._return_monitor_module_records().items():
            kept_one = False
            for module_id, source, sink in records:
                valid = (
                    key in keep_keys and source == expected.get(key)
                    and sink == "retour" and not kept_one
                )
                if valid:
                    kept_one = True
                else:
                    self._run_no_fail(["pactl", "unload-module", module_id])

    def _desired_return_monitor_sources(self, channel: ChannelConfig) -> dict[str, str]:
        linked = [str(key) for key in getattr(channel, "linked_channels", []) or []]
        desired: dict[str, str] = {}
        seen_sources: set[str] = set()

        for raw_key in linked:
            key_lower = raw_key.lower()
            source = RETURN_MIC_MONITOR_STATIC_SOURCE_BY_KEY.get(key_lower, "")

            if not source and key_lower.startswith(RETURN_MIC_MONITOR_SOURCE_PREFIX):
                source = raw_key.split(":", 1)[1]

            if not source and self._source_exists(raw_key):
                source = raw_key

            # Glass already owns the Soundboard -> MIC OUT route. Loading it
            # again here doubles that signal and can cause a sudden loud peak.
            if key_lower == "soundboard" or source in {
                "soundboard.monitor", "retour.monitor", "retour", "micro_bus.monitor",
            }:
                continue
            if source and source not in seen_sources and self._source_exists(source):
                desired[raw_key] = source
                seen_sources.add(source)

        return desired



    def _ensure_return_monitor_loopback(self, *, key: str, source_name: str) -> bool:
        media_name = self._return_monitor_media_name(key)
        existing = self._return_monitor_existing_module_ids().get(key, [])
        if existing:
            # Cleanup has already removed duplicates and stale endpoints. If
            # unloading failed, do not accept the surviving, incorrect route.
            records = self._return_monitor_module_records().get(key, [])
            if len(records) == 1 and records[0][1:] == (source_name, "retour"):
                return True
            return False

        is_monitor_source = source_name.endswith(".monitor") or source_name == "micro"
        source_is_mono = False if is_monitor_source else self._source_is_mono_1ch(source_name)

        args = [
            "pactl",
            "load-module",
            "module-loopback",
            f"source={source_name}",
            "sink=retour",
            "latency_msec=60" if source_is_mono else "latency_msec=20",
            "source_dont_move=true",
            "sink_dont_move=true",
        ]

        # Monitor sources are stereo. Physical microphone sources may be mono;
        # forcing channels=2 on those can cause robotic/doubled monitoring.
        if is_monitor_source:
            args.append("channels=2")
        elif source_is_mono:
            args.extend(["channels=1", "channel_map=mono"])

        args.append(f"sink_input_properties=media.name={media_name}")

        proc = self._run(args)
        if proc.returncode != 0 or not proc.stdout.strip():
            # Keep the monitor muted and make PipeWire load failures visible
            # in a local diagnostic without changing other audio routes.
            try:
                diagnostic = self.runtime_dir / "mic-out-monitor-error.log"
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    "MIC OUT loopback failed: "
                    + f"source={source_name!r}; returncode={proc.returncode}; "
                    + f"stderr={str(proc.stderr or '').strip()[:1000]}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            return False
        return True

    def _return_mic_final_playback_ids(self) -> list[str]:
        # The monitor is consumed by the EQ filter-chain in fallback mode.
        # Controlling the virtual sink itself does NOT guarantee attenuation
        # of an active retour.monitor capture. This is the actual headphone
        # playback stream, observed as K-Sound Hub RETURN-MIC EQ on IP14n4.
        return self._find_sink_input_ids_by_media_name("K-Sound Hub RETURN-MIC EQ")

    def _apply_return_mic_final_output_controls(
        self, channel: ChannelConfig | None, *, force_mute: bool = False,
    ) -> bool:
        """One gain/mute stage at the *audible* fallback playback endpoint.

        Fail closed: if the final stream is absent or any pactl control fails,
        do not allow the MICRO monitoring loopback to be installed/unmuted.
        This applies to slider/mute updates as well as first source selection.
        No input-source gain or Soundboard route is modified here.
        """
        ids = self._return_mic_final_playback_ids()
        if not ids:
            self._return_mic_final_muted = True
            self._return_mic_final_playback_ids_seen = ()
            return False
        # A newly spawned EQ stream may have its own default 100% gain.
        # Treat it like a first activation, never like a continuation of the
        # previously controlled sink-input id.
        current_ids = tuple(sorted(ids))
        if current_ids != getattr(self, "_return_mic_final_playback_ids_seen", ()):
            self._return_mic_final_muted = True
        volume = max(0, min(180, int(channel.volume))) if channel is not None else 0
        want_mute = (
            force_mute or channel is None or not bool(channel.enabled)
            or bool(channel.muted) or volume == 0
        )

        def apply(command: str, ident: str, value: str) -> bool:
            try:
                return self._run(["pactl", command, ident, value]).returncode == 0
            except Exception:
                return False

        # A stream that was missing from discovery cannot be treated as safe;
        # when changing sources the caller removes/avoids MICRO loopbacks.
        if want_mute:
            muted_results = [apply("set-sink-input-mute", ident, "1") for ident in ids]
            if not all(muted_results):
                self._return_mic_final_muted = True
                return False
            safe_volume = "0%" if force_mute else f"{volume}%"
            if not all(apply("set-sink-input-volume", ident, safe_volume) for ident in ids):
                self._return_mic_final_muted = True
                return False
            self._return_mic_final_muted = True
            self._return_mic_final_playback_ids_seen = current_ids
            return True

        if bool(getattr(self, "_return_mic_final_muted", True)):
            # First manual unmute: open at zero before any positive gain, then
            # ramp the FINAL output alone. A new source stays muted until here.
            muted_results = [apply("set-sink-input-mute", ident, "1") for ident in ids]
            if not all(muted_results):
                self._return_mic_final_muted = True
                return False
            if not all(apply("set-sink-input-volume", ident, "0%") for ident in ids):
                self._return_mic_final_muted = True
                return False
            if not all(apply("set-sink-input-mute", ident, "0") for ident in ids):
                self._return_mic_final_muted = True
                return False
            for fraction in (0.25, 0.5, 0.75, 1.0):
                step = max(0, min(180, round(volume * fraction)))
                if not all(apply("set-sink-input-volume", ident, f"{step}%") for ident in ids):
                    for ident in ids:
                        apply("set-sink-input-mute", ident, "1")
                    self._return_mic_final_muted = True
                    return False
                if fraction < 1.0:
                    time.sleep(0.015)
        else:
            if not all(apply("set-sink-input-volume", ident, f"{volume}%") for ident in ids):
                for ident in ids:
                    apply("set-sink-input-mute", ident, "1")
                self._return_mic_final_muted = True
                return False
            if not all(apply("set-sink-input-mute", ident, "0") for ident in ids):
                self._return_mic_final_muted = True
                return False
        self._return_mic_final_muted = False
        self._return_mic_final_playback_ids_seen = current_ids
        return True

    def _sync_return_mic_fallback_eq(self, settings: AppSettings) -> None:
        """Keep the fallback MIC OUT EQ/target in sync without touching peers.

        The ordinary channel apply used to reconcile only the monitor source.
        If MIC OUT's playback target or EQ changed, its old filter-chain could
        remain on the previous headset until the next application restart.
        Mute the real playback endpoint AND the input bus before any restart;
        a new EQ stream can initially have Pulse's restored 100% gain.
        """
        if self._native_playback_enabled():
            return
        channel = self._find_channel(settings, "return-mic")
        slot = self.eq_slots.get("return-mic")
        if channel is None or slot is None:
            return

        target = self._resolve_playback_target(channel)
        if target is None:
            return
        signature = _build_eq_slot_signature(
            "return-mic", self._current_profile(channel), target, channel.stereo_width,
        )
        if (
            slot.proc is not None and slot.proc.poll() is None
            and slot.signature == signature
        ):
            return

        # A preserved UI handoff adopts a live filter chain with a blank
        # signature. Do not kill the preserved audio on the first slider tick.
        if (
            getattr(self, "_reuse_preserved_audio_runtime", False)
            and slot.proc is not None and slot.proc.poll() is None
            and not slot.signature
        ):
            slot.signature = signature
            return

        self._apply_return_mic_final_output_controls(channel, force_mute=True)
        self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
        # For MIC OUT the EQ module must NOT apply channel gain to "retour";
        # _apply_return_mic_final_output_controls owns gain/mute exclusively.
        self._apply_eq_slot(settings, "return-mic")

        # Wait only during an actual rebuild, not for each slider update.
        # A stream that has not appeared yet will fail closed in _apply_return_mic.
        for _ in range(6):
            if self._return_mic_final_playback_ids():
                break
            time.sleep(0.05)

    def _return_mic_has_independent_soundboard_input(self) -> bool:
        """Treat only the configured, live Glass-owned pad route as MIC OUT input.

        Do not infer an active route from a saved selector alone, and do not
        create a second soundboard.monitor loopback in the MIC OUT engine.
        """
        try:
            configured = getattr(self, "_soundboard_config_path", None)
            path = Path(configured) if configured else Path.home() / ".config/k-sounds-hub/soundboard.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return False
            output = str(document.get("output_channel") or "").strip().lower().replace("_", "-")
            selected = output in {"return-mic", "retour", "mic-out", "mic out"}
            if not output:
                selected = document.get("monitor_to_mic_out") is True
            if not selected:
                return False
            modules = self._run(["pactl", "list", "short", "modules"])
            if modules.returncode != 0:
                return False
            return any(
                "module-loopback" in line
                and "source=soundboard.monitor" in line
                and "sink=retour" in line
                and "K-Sound-Hub-Soundboard-To-Output" in line
                for line in modules.stdout.splitlines()
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _apply_return_mic(self, settings: AppSettings) -> None:
        # IP14n9d: a separate single-owner controller replaces the old MIC OUT
        # loopback/handoff code in native-monitor mode. Legacy code below is
        # available ONLY when explicitly launching without the experiment flag.
        if (
            getattr(self, "_native_monitor_requested", lambda: False)()
            and getattr(self, "_native_micro_enabled", lambda: False)()
        ):
            from ksound_hub.audio.mic_out_native import reconcile_native_mic_out
            reconcile_native_mic_out(self, settings)
            return
        channel = self._find_channel(settings, "return-mic")
        native_micro_available = bool(getattr(self, "_native_micro_enabled", lambda: False)())

        # MIC OUT: optionally tap the native MICRO final mix before micro_bus.
        # Remaining inputs still enter retour via existing independent routes.
        if not self._return_mic_legacy_cleanup_done:
            self._disconnect_return_playback_links()
            self._cleanup_legacy_return_playback_modules()

            for module_id in self._find_loopback_module_ids_by_media_name("K-Sounds Hub Mic Output Monitor Capture"):
                self._run_no_fail(["pactl", "unload-module", module_id])

            self._return_mic_legacy_cleanup_done = True

        if channel is None or not channel.enabled or int(channel.volume) <= 0:
            self._native_mic_out_route_allowed = False
            self._native_mic_out_native_sources = {}
            if native_micro_available:
                self._write_native_micro_state(settings)
            if not self._native_playback_enabled():
                self._apply_return_mic_final_output_controls(channel, force_mute=True)
            self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
            self._cleanup_return_monitor_sources()
            self._return_mic_runtime.capture_module_id = ""
            self._return_mic_runtime.applied_signature = ""
            return

        if not self._sink_exists("retour"):
            self._run_no_fail([
                "pactl",
                "load-module",
                "module-null-sink",
                "sink_name=retour",
                "sink_properties=device.description=🎧MIC OUT",
            ])

        if not self._sink_exists("retour"):
            self._native_mic_out_route_allowed = False
            self._native_mic_out_native_sources = {}
            if native_micro_available:
                self._write_native_micro_state(settings)
            self._cleanup_return_monitor_sources()
            self._return_mic_runtime.capture_module_id = ""
            self._return_mic_runtime.applied_signature = ""
            return

        desired_all = self._desired_return_monitor_sources(channel)
        # The native feed consumes the C++ mix BEFORE it reaches virtual
        # `micro`: it must not depend on `micro` passing _source_exists().
        # The selected link, not the virtual source's startup timing, decides.
        native_monitor = bool(
            getattr(self, "_native_monitor_eligible", lambda _ch: False)(channel)
        )
        # In experimental native mode, MIC OUT owns NO source loopbacks:
        # MICRO uses the processed C++ tap; other capture sources use that
        # engine's existing native return_source transport instead. Soundboard
        # -> retour is a separate owner and is deliberately not touched.
        native_requested = bool(
            getattr(self, "_native_monitor_requested", lambda: native_monitor)()
        )
        native_path = native_micro_available and native_requested
        native_sources = (
            {key: source for key, source in desired_all.items() if source != "micro"}
            if native_path and not native_monitor else {}
        )
        desired = {} if native_path else desired_all
        # The Glass-owned Soundboard -> retour input is deliberately absent
        # from MIC OUT-owned desired routes. Count it without duplicating it.
        has_input = (
            bool(desired) or native_monitor or bool(native_sources)
            or self._return_mic_has_independent_soundboard_input()
        )
        expected_native = (
            {"native:micro": "micro (native)"} if native_monitor else
            {f"native:{key}": source for key, source in native_sources.items()}
        )
        self._return_mic_expected_sources = {**desired, **expected_native}
        current_keys = set(self._return_monitor_existing_module_ids())
        desired_keys = set(desired)
        # Detect the same logical key bound to a stale source, duplicates, and
        # failed unloads: key equality alone was not a valid route check.
        records_method = getattr(self, "_return_monitor_module_records", None)
        records = records_method() if callable(records_method) else {}
        route_mismatch = any(
            len(records.get(key, [])) != 1
            or records[key][0][1:] != (source_name, "retour")
            for key, source_name in desired.items()
        ) if records else False
        will_change = (
            current_keys != desired_keys or route_mismatch
            or any(len(ids) != 1 for ids in self._return_monitor_existing_module_ids().values())
            or (native_monitor or bool(native_sources)) != bool(getattr(self, "_native_mic_out_route_allowed", False))
            or native_sources != dict(getattr(self, "_native_mic_out_native_sources", {}) or {})
        )

        # Stop the previous native feed before changing routes. This prevents
        # a brief overlap with the old micro->retour loopback on transitions.
        if will_change:
            self._native_mic_out_route_allowed = False
            self._native_mic_out_native_sources = {}
            if native_micro_available:
                self._write_native_micro_state(settings)

        original_muted = bool(channel.muted)
        native_playback = self._native_playback_enabled()

        # Critical fallback path: silence the actual playback EQ stream BEFORE
        # loading/changing a MIC OUT loopback. A muted virtual sink is not
        # sufficient when another node captures retour.monitor.
        if not native_playback:
            if not self._apply_return_mic_final_output_controls(
                channel, force_mute=will_change or not has_input,
            ):
                self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
                self._cleanup_return_monitor_sources()
                self._return_mic_runtime.capture_module_id = ""
                self._return_mic_runtime.applied_signature = ""
                return

        if will_change:
            # Muting only the Pulse sink is not enough because the V2 engine
            # captures retour.monitor. Temporarily mute the rendered MIC OUT
            # channel in native state before changing PipeWire loopbacks.
            if native_playback:
                channel.muted = True
                self._write_v2_state(settings)
                self._write_v2_volume_state(settings)
                self._ensure_v2_engine()
            self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
            time.sleep(0.12)

        self._cleanup_return_monitor_sources(keep_keys=desired_keys)

        all_created = True
        for key, source_name in desired.items():
            if not self._ensure_return_monitor_loopback(key=key, source_name=source_name):
                all_created = False

        # On failed unload, do not start a second route alongside the stale one.
        if native_path and any(
            records for records in self._return_monitor_module_records().values()
        ):
            all_created = False

        if not all_created:
            # Never open the bus after an invalid or partially created route.
            self._native_mic_out_route_allowed = False
            self._native_mic_out_native_sources = {}
            if native_micro_available:
                self._write_native_micro_state(settings)
            self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
            if not native_playback:
                self._apply_return_mic_final_output_controls(channel, force_mute=True)
            self._cleanup_return_monitor_sources()
            self._return_mic_runtime.capture_module_id = ""
            self._return_mic_runtime.applied_signature = ""
            return

        if will_change:
            time.sleep(0.12)

        # Only the native engine produces this feed, and only after stale
        # MICRO loopbacks have been removed successfully. If it cannot start,
        # keep the final endpoint closed rather than opening a silent route.
        self._native_mic_out_route_allowed = native_monitor or bool(native_sources)
        self._native_mic_out_native_sources = native_sources
        if native_micro_available:
            self._write_native_micro_state(settings)
            if (native_monitor or native_sources) and not self._ensure_native_micro_engine():
                self._native_mic_out_route_allowed = False
                self._native_mic_out_native_sources = {}
                self._write_native_micro_state(settings)
                self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
                if not native_playback:
                    self._apply_return_mic_final_output_controls(channel, force_mute=True)
                self._return_mic_runtime.applied_signature = ""
                return

        # Keep the virtual mix at unity; in fallback the FINAL EQ playback
        # stream, never this intermediate sink, owns the user gain and mute.
        # A muted null sink can produce SILENCE on retour.monitor: leaving this
        # bus muted while unmuting RETURN-MIC EQ makes MIC OUT inaudible even
        # though the exported MICRO and its loopback are both healthy.
        self._run_no_fail(["pactl", "set-sink-volume", "retour", "100%"])
        if native_playback:
            # Restore the *original* user mute, not temporary route-change mute.
            self._run_no_fail(["pactl", "set-sink-mute", "retour", "1" if original_muted else "0"])
            if will_change:
                channel.muted = original_muted
                self._write_v2_state(settings)
                self._write_v2_volume_state(settings)
                # IP14n9a wrote native_monitor=0 while channel.muted=True
                # during the safety handoff, but never refreshed the C++
                # state after restoring the user's original mute setting.
                # Keep the C++ tap and final renderer synchronized.
                if native_micro_available:
                    self._write_native_micro_state(settings)
        else:
            if not has_input:
                # Off must remain Off, including the independently owned
                # Soundboard -> retour loopback. Never open an unmanaged path.
                self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
                self._apply_return_mic_final_output_controls(channel, force_mute=True)
            else:
                # This is an INPUT bus for the EQ, not the final headphone
                # endpoint. Before opening it, verify and silence that endpoint.
                # Its own 0->configured-volume ramp handles manual unmute.
                sink_mute = self._run(["pactl", "get-sink-mute", "retour"])
                if sink_mute.returncode != 0 or not str(sink_mute.stdout).strip():
                    self._apply_return_mic_final_output_controls(channel, force_mute=True)
                    self._cleanup_return_monitor_sources()
                    self._return_mic_runtime.capture_module_id = ""
                    self._return_mic_runtime.applied_signature = ""
                    return
                bus_muted = "yes" in sink_mute.stdout.lower()
                if bus_muted:
                    if not self._apply_return_mic_final_output_controls(channel, force_mute=True):
                        self._cleanup_return_monitor_sources()
                        self._return_mic_runtime.capture_module_id = ""
                        self._return_mic_runtime.applied_signature = ""
                        return
                    opened = self._run(["pactl", "set-sink-mute", "retour", "0"])
                    if opened.returncode != 0:
                        self._apply_return_mic_final_output_controls(channel, force_mute=True)
                        self._cleanup_return_monitor_sources()
                        self._return_mic_runtime.capture_module_id = ""
                        self._return_mic_runtime.applied_signature = ""
                        return
                if not self._apply_return_mic_final_output_controls(channel):
                    self._run_no_fail(["pactl", "set-sink-mute", "retour", "1"])
                    self._apply_return_mic_final_output_controls(channel, force_mute=True)
                    self._cleanup_return_monitor_sources()
                    self._return_mic_runtime.capture_module_id = ""
                    self._return_mic_runtime.applied_signature = ""
                    return

        signature = json.dumps(
            {
                "return_standard_channel": True,
                "sources": sorted(desired.items()),
                "native_micro_monitor": native_monitor,
                "native_sources": sorted(native_sources.items()),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        self._return_mic_runtime.capture_module_id = ""
        self._return_mic_runtime.applied_signature = signature

    def _native_engine_binary(self) -> Path:
        return Path(__file__).resolve().parents[3] / "native_engine" / "build" / "ksound_native_engine"

    def _activate_v2_fallback(
        self,
        reason: str,
        settings: AppSettings | None = None,
    ) -> None:
        if self._v2_fallback_reason:
            return

        self._v2_fallback_reason = reason.strip() or "native playback unavailable"
        self._stop_v2_engine()
        active_settings = settings or self._last_playback_settings
        if active_settings is None:
            for slot in self.eq_slots.values():
                slot.status = "legacy playback safe fallback"
            return

        for key in PLAYBACK_KEYS:
            PipeWireAudioEngineBase._apply_playback_channel(self, active_settings, key)

    def _record_v2_engine_failure(self, reason: str) -> None:
        now = time.monotonic()
        self._v2_engine_failures = [
            stamp
            for stamp in self._v2_engine_failures
            if now - stamp <= NATIVE_PLAYBACK_FAILURE_WINDOW_SECONDS
        ]
        self._v2_engine_failures.append(now)
        if len(self._v2_engine_failures) >= NATIVE_PLAYBACK_FAILURE_LIMIT:
            self._activate_v2_fallback(reason)

    def _ensure_v2_engine(self) -> bool:
        if not self._native_playback_enabled():
            self._stop_v2_engine()
            return False

        if self._v2_engine_proc is not None:
            return_code = self._v2_engine_proc.poll()
            if return_code is None:
                return True
            self._v2_engine_proc = None
            self._record_v2_engine_failure(f"native engine exited {return_code}")
            if not self._native_playback_enabled():
                return False

        engine_bin = self._native_engine_binary()
        if not engine_bin.is_file():
            self._activate_v2_fallback("native playback binary missing")
            return False
        env = os.environ.copy()
        env["KSH_RUNTIME_ROLE"] = "native_engine_real"
        self._v2_engine_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._v2_engine_log.open("ab", buffering=0) as log_file:
                self._v2_engine_proc = subprocess.Popen(
                    [
                        str(engine_bin),
                        "--state",
                        str(self._v2_state_path),
                        "--volume-state",
                        str(self._v2_volume_state_path),
                        "--levels",
                        str(self._v2_levels_path),
                        "--log",
                        str(self._v2_engine_log),
                        "--period-ms",
                        "20",
                    ],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
        except Exception as exc:
            self._v2_engine_proc = None
            self._activate_v2_fallback(f"native engine start failed: {type(exc).__name__}")
            return False
        return True

    def _stop_v2_engine(self) -> None:
        proc = self._v2_engine_proc
        self._v2_engine_proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
                try:
                    proc.wait(timeout=1.2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
        except Exception:
            pass

    def _logical_sink_for_playback_key(self, key: str) -> str:
        return PLAYBACK_EQ_CHANNELS.get(key, key)

    def _apply_playback_controls_to_visible_sink(self, channel: ChannelConfig) -> None:
        sink_name = self._logical_sink_for_playback_key(channel.key)
        if channel.key in PLAYBACK_KEYS and sink_name and self._sink_exists(sink_name):
            self._set_node_volume_smooth(node_type="sink", node_name=sink_name, target_volume=100)
            self._run_no_fail(["pactl", "set-sink-mute", sink_name, "0"])

    def _apply_playback_channel(self, settings: AppSettings, channel_key: str) -> None:
        if not self._native_playback_enabled():
            PipeWireAudioEngineBase._apply_playback_channel(self, settings, channel_key)
            return

        channel = self._find_channel(settings, channel_key)
        if channel is not None:
            self._apply_playback_controls_to_visible_sink(channel)
        if channel_key == "return-mic":
            self._apply_return_mic(settings)
        self._write_v2_state(settings)
        self._write_v2_volume_state(settings)
        self._ensure_v2_engine()
        slot = self.eq_slots[channel_key]
        slot.status = "v2 native final-render active"

    def apply_channel_volume_realtime(self, settings: AppSettings, channel_key: str) -> None:
        """Ultra-light continuous volume apply for UI/shortcut ramps.

        This path intentionally avoids synchronous pactl enumeration/control work
        while a volume gesture is in flight. Native engines consume the same
        state files as the normal fast path, so audible volume follows the UI
        target without making the Qt GUI thread wait on PipeWire/Pulse commands.
        A normal/full apply is still performed when the gesture finishes.
        """
        self._last_playback_settings = settings

        if channel_key in PLAYBACK_KEYS and self._native_playback_enabled():
            if channel_key in self.eq_slots:
                self.eq_slots[channel_key].status = "v2 native final-render active"
            self._write_v2_volume_state(settings)

            # If playback is routed into the native MICRO engine, mirror the
            # newest slider gains through its lightweight state file too. Do
            # not run pactl send discovery/updates for every animation frame.
            if channel_key in {"all", "game", "chat", "media", "more"} and self._native_micro_enabled():
                self._write_native_micro_state(settings)
                self._ensure_native_micro_engine()

            self._ensure_v2_engine()
            return

        if channel_key == "micro" and self._native_micro_enabled():
            self._write_native_micro_state(settings)
            self._ensure_native_micro_engine()
            return

        if channel_key == "return-mic" and not self._native_playback_enabled():
            # Continuous slider updates only touch the actual headphone EQ
            # stream. Do not enumerate/rebuild the whole PipeWire route at
            # 60 Hz; a final full apply still reconciles devices and modules.
            channel = self._find_channel(settings, "return-mic")
            if channel is not None:
                native_mode = getattr(self, "_native_monitor_requested", lambda: False)()
                ready = (
                    bool(self._return_mic_runtime.applied_signature)
                    if native_mode else
                    bool(getattr(self, "_return_mic_expected_sources", {}))
                    and bool(self._return_mic_runtime.applied_signature)
                )
                self._apply_return_mic_final_output_controls(
                    channel, force_mute=not ready,
                )
            return

        # Non-native/fallback configurations retain the established behavior.
        self.apply_channel_volume_fast(settings, channel_key)

    def apply_channel_volume_fast(self, settings: AppSettings, channel_key: str) -> None:
        if channel_key == "micro" and self._native_micro_enabled():
            channel = self._find_channel(settings, "micro")
            if channel is None:
                return

            self._write_native_micro_state(settings)
            self._apply_micro_link_send_volumes(settings)
            self._unload_legacy_ksh_mic_physical_loopbacks()
            self._ensure_native_micro_engine()
            self._unload_legacy_ksh_mic_physical_loopbacks()
            self._apply_micro_endpoint_controls(channel)
            self._apply_return_mic(settings)
            return

        if not self._native_playback_enabled():
            self.apply_channel(settings, channel_key)
            return

        if channel_key == "micro":
            channel = self._find_channel(settings, "micro")
            if channel is None:
                return

            if self._native_micro_enabled():
                self._write_native_micro_state(settings)
                self._apply_micro_link_send_volumes(settings)
                self._ensure_native_micro_engine()
                self._apply_micro_endpoint_controls(channel)
                self._apply_return_mic(settings)
                return

            # Fallback PipeWire loopback path:
            # physical mic source -> KSH_MIC_PHYSICAL sink-input -> micro_bus -> micro source.
            # Keep the physical/EasyEffects feed at unity. The user-facing
            # MICRO slider is applied to the exported virtual source "micro"
            # by _apply_micro_endpoint_controls().
            muted = "1" if self._micro_channel_is_muted(channel) else "0"

            sink_input_ids = self._find_sink_input_ids_by_media_name("KSH_MIC_PHYSICAL")
            if sink_input_ids:
                for sink_input_id in sink_input_ids:
                    self._run_no_fail(["pactl", "set-sink-input-volume", sink_input_id, "100%"])
                    self._run_no_fail(["pactl", "set-sink-input-mute", sink_input_id, muted])
                self._apply_micro_endpoint_controls(channel)
                self._apply_return_mic(settings)
                return

            # Rare fallback: if the loopback is missing, fall back to the normal
            # micro apply path so the transport can be recreated.
            self.apply_channel(settings, channel_key)
            return

        if channel_key not in PLAYBACK_KEYS:
            self.apply_channel(settings, channel_key)
            return

        if channel_key in self.eq_slots:
            self.eq_slots[channel_key].status = "v2 native final-render active"

        self._write_v2_volume_state(settings)

        if channel_key in {"all", "game", "chat", "media", "more"}:
            # If this playback channel is shared into MICRO, update that send too.
            self._write_native_micro_state(settings)
            self._apply_micro_link_send_volumes(settings)

        self._ensure_v2_engine()

    def apply_channel(self, settings: AppSettings, channel_key: str) -> None:
        if not self._native_playback_enabled():
            if channel_key == "return-mic":
                # The base class treats return-mic as a generic playback/EQ
                # channel first and never reaches _apply_return_mic_channel().
                # Route monitoring through the V2 single-owner path even when
                # native final-render playback is disabled/safely falling back.
                reconcile = getattr(self, "_sync_return_mic_fallback_eq", None)
                if callable(reconcile):
                    reconcile(settings)
                self._apply_return_mic(settings)
            elif channel_key == "micro" and self._native_micro_enabled():
                self._apply_native_micro_channel(settings)
            else:
                PipeWireAudioEngineBase.apply_channel(self, settings, channel_key)
            return

        if channel_key == "return-mic":
            # MIC OUT is also rendered by the V2 playback engine, but its
            # monitored sources are managed by _apply_return_mic(). It must be
            # applied before the generic PLAYBACK_KEYS fast path, otherwise
            # source add/remove changes only take effect after restart.
            self._apply_return_mic(settings)
            if channel_key in self.eq_slots:
                self.eq_slots[channel_key].status = "v2 native final-render active"
            self._write_v2_state(settings)
            self._write_v2_volume_state(settings)
            self._ensure_v2_engine()
            return

        if channel_key in PLAYBACK_KEYS:
            # V2 playback volume/mute/EQ/target are handled by the native
            # final-render engine. Do not call the legacy playback path here:
            # it performs synchronous pactl work and makes sliders lag.
            if channel_key in self.eq_slots:
                self.eq_slots[channel_key].status = "v2 native final-render active"
            self._write_v2_state(settings)
            self._write_v2_volume_state(settings)
            self._ensure_v2_engine()
            return

        if channel_key == "micro":
            if self._native_micro_enabled():
                self._apply_native_micro_channel(settings)
                return

            channel = self._find_channel(settings, "micro")
            if channel is not None:
                self._ensure_physical_micro_loopbacks(channel)
                self._run_no_fail(["pactl", "set-default-source", "micro"])
                self._apply_micro_endpoint_controls(channel)
                self._apply_return_mic(settings)
            return

        channel = self._find_channel(settings, channel_key)
        if channel is None:
            return
        control = CONTROL_NODE_BY_CHANNEL.get(channel_key)
        if control is None:
            return
        node_type, node_name = control
        self._apply_node_controls(channel, node_type=node_type, node_name=node_name)

    def apply_settings(self, settings: AppSettings) -> None:
        if not self._native_playback_enabled():
            self._stop_v2_engine()
            if self._native_micro_enabled():
                # Keep legacy playback/EQ in safe mode, but do not call the
                # base MICRO apply: it would create KSH_MIC_PHYSICAL beside the
                # independently enabled native MICRO transport.
                for key in PLAYBACK_KEYS:
                    self._apply_eq_slot(settings, key)
                self._apply_native_micro_channel(settings)
            else:
                self._stop_native_micro_engine()
                PipeWireAudioEngineBase.apply_settings(self, settings)
            return

        for key in PLAYBACK_KEYS:
            channel = self._find_channel(settings, key)
            if channel is not None:
                self._apply_playback_controls_to_visible_sink(channel)
                self.eq_slots[key].status = "v2 native final-render active"
        self._apply_return_mic(settings)
        self._write_v2_state(settings)
        self._write_v2_volume_state(settings)
        self._ensure_v2_engine()

        channel = self._find_channel(settings, "micro")
        if channel is not None:
            node_names = self._resolved_micro_source_names(channel)
            if not node_names:
                node_names = [self._resolved_micro_source_name(channel)]
            for node_name in node_names:
                self._apply_node_controls(channel, node_type="source", node_name=node_name)
            if not self._native_micro_enabled():
                self._ensure_physical_micro_loopbacks(channel)
            self._run_no_fail(["pactl", "set-default-source", "micro"])
        self._apply_micro_transport(settings)
        if channel is not None:
            self._apply_micro_endpoint_controls(channel)
        self._apply_return_mic(settings)

    def _read_v2_levels_payload(self) -> dict[str, Any]:
        try:
            stat = self._v2_levels_path.stat()
            mtime_ns = int(stat.st_mtime_ns)

            # A dead/stopped producer must not leave its final meter frame
            # displayed forever. Native producers update far faster than this.
            if max(0, time.time_ns() - mtime_ns) > 500_000_000:
                self._v2_levels_cache_mtime_ns = mtime_ns
                self._v2_levels_cache_payload = {}
                return {}

            if mtime_ns == self._v2_levels_cache_mtime_ns:
                return self._v2_levels_cache_payload

            payload = json.loads(self._v2_levels_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}

            self._v2_levels_cache_mtime_ns = mtime_ns
            self._v2_levels_cache_payload = payload
            return payload
        except Exception:
            self._v2_levels_cache_payload = {}
            return {}

    def _read_native_micro_levels_payload(self) -> dict[str, Any]:
        try:
            stat = self._native_micro_levels_path.stat()
            mtime_ns = int(stat.st_mtime_ns)

            if max(0, time.time_ns() - mtime_ns) > 500_000_000:
                self._native_micro_levels_cache_mtime_ns = mtime_ns
                self._native_micro_levels_cache_payload = {}
                return {}

            if mtime_ns == self._native_micro_levels_cache_mtime_ns:
                return self._native_micro_levels_cache_payload

            payload = json.loads(self._native_micro_levels_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {}

            self._native_micro_levels_cache_mtime_ns = mtime_ns
            self._native_micro_levels_cache_payload = payload
            return payload
        except Exception:
            self._native_micro_levels_cache_payload = {}
            return {}

    def native_micro_processing_activity(self) -> dict[str, float]:
        """Return lightweight native DSP gain-reduction telemetry.

        The native MICRO engine already writes micro_levels.json for meters, so
        this adds no extra native worker or polling loop. Callers decide when
        to read it (the MICRO Processing dialog does so only while visible).
        """
        if not self._native_micro_enabled():
            return {
                "compressor_reduction_db": 0.0,
                "deesser_reduction_db": 0.0,
                "limiter_reduction_db": 0.0,
            }

        payload = self._read_native_micro_levels_payload()
        raw = payload.get("processing", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw, dict):
            raw = {}

        result: dict[str, float] = {}
        for key in (
            "compressor_reduction_db",
            "deesser_reduction_db",
            "limiter_reduction_db",
        ):
            try:
                value = float(raw.get(key, 0.0))
            except Exception:
                value = 0.0
            if not math.isfinite(value):
                value = 0.0
            result[key] = max(0.0, min(60.0, value))
        return result

    def meter_levels_batch(
        self,
        channel_keys,
    ) -> dict[str, tuple[float, float]]:
        # One coherent meter snapshot with minimal per-frame I/O.
        keys = tuple(dict.fromkeys(
            str(key or "").strip()
            for key in channel_keys
            if str(key or "").strip()
        ))
        if not keys:
            return {}

        base_meter = super().meter_levels

        if not self._native_playback_enabled():
            return {key: base_meter(key) for key in keys}

        self._ensure_v2_engine()
        if not self._native_playback_enabled():
            return {key: base_meter(key) for key in keys}

        need_playback = any(key in PLAYBACK_KEYS for key in keys)
        native_micro_enabled = self._native_micro_enabled()
        need_micro = native_micro_enabled and any(
            key in {"micro", "return-mic"} and key not in PLAYBACK_KEYS
            for key in keys
        )

        playback_payload = (
            self._read_v2_levels_payload()
            if need_playback
            else {}
        )
        playback_channels = (
            playback_payload.get("channels", {})
            if isinstance(playback_payload, dict)
            else {}
        )
        if not isinstance(playback_channels, dict):
            playback_channels = {}

        micro_payload = (
            self._read_native_micro_levels_payload()
            if need_micro
            else {}
        )
        micro_channels = (
            micro_payload.get("channels", {})
            if isinstance(micro_payload, dict)
            else {}
        )
        if not isinstance(micro_channels, dict):
            micro_channels = {}

        result: dict[str, tuple[float, float]] = {}

        def parsed(levels):
            if not (isinstance(levels, list) and len(levels) >= 2):
                return None
            try:
                return float(levels[0]), float(levels[1])
            except Exception:
                return (0.0, 0.0)

        for key in keys:
            if key in PLAYBACK_KEYS:
                levels = playback_channels.get(key)
                if levels is None and key == "return-mic":
                    levels = playback_channels.get("retour")
                value = parsed(levels)
                result[key] = value if value is not None else (0.0, 0.0)
                continue

            if key in {"micro", "return-mic"} and native_micro_enabled:
                value = parsed(micro_channels.get(key))
                if value is not None:
                    result[key] = value
                    continue

            result[key] = base_meter(key)

        return result

    def meter_levels(self, channel_key: str) -> tuple[float, float]:
        if not self._native_playback_enabled():
            return super().meter_levels(channel_key)

        self._ensure_v2_engine()
        if not self._native_playback_enabled():
            return super().meter_levels(channel_key)

        if channel_key in PLAYBACK_KEYS:
            payload = self._read_v2_levels_payload()
            levels = payload.get("channels", {}).get(channel_key)
            if levels is None and channel_key == "return-mic":
                levels = payload.get("channels", {}).get("retour")
            if isinstance(levels, list) and len(levels) >= 2:
                try:
                    return float(levels[0]), float(levels[1])
                except Exception:
                    return (0.0, 0.0)
            return (0.0, 0.0)

        if channel_key in {"micro", "return-mic"}:
            if not self._native_micro_enabled():
                return super().meter_levels(channel_key)

            payload = self._read_native_micro_levels_payload()
            levels = payload.get("channels", {}).get(channel_key)
            if isinstance(levels, list) and len(levels) >= 2:
                try:
                    return float(levels[0]), float(levels[1])
                except Exception:
                    return (0.0, 0.0)

            return super().meter_levels(channel_key)

        return super().meter_levels(channel_key)
