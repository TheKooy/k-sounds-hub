from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG_DIR


SOUNDBOARD_PATH = CONFIG_DIR / "soundboard.json"
SOUNDBOARD_BUS = "soundboard"
SOUNDBOARD_PLAYER_MEDIA_NAME = "K-Sounds-Hub-Soundboard-Player"


def _normalize_key(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


class SoundboardService:
    """Non-UI owner for Soundboard state, commands and player processes."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        default_columns: int = 4,
        popen_factory: Callable[..., subprocess.Popen] | None = None,
        run_factory: Callable[..., subprocess.CompletedProcess] | None = None,
        killpg: Callable[[int, int], None] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path is not None else SOUNDBOARD_PATH
        self.default_columns = max(1, int(default_columns))
        self._popen = popen_factory or subprocess.Popen
        self._run = run_factory or subprocess.run
        self._killpg = killpg or os.killpg
        self._sleep = sleep or time.sleep
        self._players: list[subprocess.Popen] = []
        self._lock = threading.RLock()

    def read_document(self) -> dict[str, Any]:
        with self._lock:
            return self._read_document_unlocked()

    def _read_document_unlocked(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {"slots": []}

        try:
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {"slots": []}

        if isinstance(loaded, list):
            return {"slots": loaded}
        if not isinstance(loaded, dict):
            return {"slots": []}

        if not isinstance(loaded.get("slots"), list):
            loaded["slots"] = []
        return loaded

    def write_document(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError("soundboard document must be a dictionary")

        with self._lock:
            payload = dict(data)
            if not isinstance(payload.get("slots"), list):
                payload["slots"] = []

            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_name(
                f".{self.config_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.config_path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass

    def read_slots(self) -> list[dict[str, Any]]:
        data = self.read_document()
        return [slot for slot in data.get("slots", []) if isinstance(slot, dict)]

    def read_global_volume(self) -> int:
        try:
            value = int(self.read_document().get("global_volume", 100))
        except Exception:
            value = 100
        return max(0, min(150, value))

    def set_global_volume(self, value: object) -> bool:
        try:
            volume = max(0, min(100, int(value)))
        except Exception:
            return False

        with self._lock:
            data = self._read_document_unlocked()
            data["global_volume"] = volume
            self.write_document(data)
        return True

    def read_auto_level_enabled(self) -> bool:
        return bool(self.read_document().get("auto_level_enabled", False))

    def set_auto_level_enabled(self, enabled: object) -> None:
        with self._lock:
            data = self._read_document_unlocked()
            data["auto_level_enabled"] = bool(enabled)
            self.write_document(data)

    def slot_for_key(self, key: object) -> dict[str, Any] | None:
        wanted = _normalize_key(key)
        if not wanted:
            return None

        for index, slot in enumerate(self.read_slots()):
            aliases = {
                _normalize_key(slot.get("id")),
                _normalize_key(slot.get("label")),
                _normalize_key(index + 1),
                _normalize_key(f"sb{index + 1}"),
            }
            if wanted in aliases:
                return slot
        return None

    def move_slot_by_key(
        self,
        key: object,
        direction: object,
        *,
        columns: int | None = None,
    ) -> bool:
        wanted = _normalize_key(key)
        move = str(direction or "").strip().lower()
        if not wanted or move not in {"left", "right", "up", "down"}:
            return False

        with self._lock:
            data = self._read_document_unlocked()
            slots = [slot for slot in data.get("slots", []) if isinstance(slot, dict)]
            index = -1
            for candidate_index, slot in enumerate(slots):
                aliases = {
                    _normalize_key(slot.get("id")),
                    _normalize_key(slot.get("label")),
                    _normalize_key(candidate_index + 1),
                    _normalize_key(f"sb{candidate_index + 1}"),
                }
                if wanted in aliases:
                    index = candidate_index
                    break

            if index < 0:
                return False

            stored_columns = data.get("remote_columns", 0)
            try:
                column_count = int(columns or stored_columns or self.default_columns)
            except (TypeError, ValueError):
                column_count = self.default_columns
            column_count = max(1, min(13, column_count))
            column = index % column_count
            target = -1
            if move == "left" and column > 0:
                target = index - 1
            elif move == "right" and column < column_count - 1 and index + 1 < len(slots):
                target = index + 1
            elif move == "up" and index - column_count >= 0:
                target = index - column_count
            elif move == "down" and index + column_count < len(slots):
                target = index + column_count

            if target < 0 or target >= len(slots):
                return False

            slots[index], slots[target] = slots[target], slots[index]
            data["slots"] = slots
            self.write_document(data)
        return True

    def reorder_slots_by_ids(self, order: object) -> bool:
        if not isinstance(order, list):
            return False

        with self._lock:
            data = self._read_document_unlocked()
            slots = [slot for slot in data.get("slots", []) if isinstance(slot, dict)]
            by_id = {
                str(slot.get("id") or "").strip(): slot
                for slot in slots
                if str(slot.get("id") or "").strip()
            }
            used: set[str] = set()
            reordered: list[dict[str, Any]] = []

            for raw_id in order:
                slot_id = str(raw_id or "").strip()
                if slot_id and slot_id in by_id and slot_id not in used:
                    reordered.append(by_id[slot_id])
                    used.add(slot_id)

            for slot in slots:
                slot_id = str(slot.get("id") or "").strip()
                if slot_id not in used:
                    reordered.append(slot)

            if len(reordered) != len(slots):
                return False
            if [slot.get("id") for slot in reordered] == [slot.get("id") for slot in slots]:
                return True

            data["slots"] = reordered
            self.write_document(data)
        return True

    def play_slot_by_key(self, key: object) -> bool:
        slot = self.slot_for_key(key)
        return bool(slot and self.play_slot(slot))

    def play_slot(self, slot: dict[str, Any]) -> bool:
        if not isinstance(slot, dict):
            return False

        path = Path(str(slot.get("path") or "")).expanduser()
        if not path.is_file():
            return False

        root = self.read_document()
        try:
            global_volume = max(0, min(150, int(root.get("global_volume", 100))))
        except Exception:
            global_volume = 100
        try:
            slot_volume = max(0, min(150, int(slot.get("volume", 100))))
        except Exception:
            slot_volume = 100

        if bool(root.get("auto_level_enabled", False)):
            try:
                auto_gain = max(0.05, min(1.5, float(slot.get("auto_gain", 1.0) or 1.0)))
            except Exception:
                auto_gain = 1.0
        else:
            auto_gain = 1.0

        try:
            trim_db = max(-24.0, min(24.0, float(slot.get("trim_db", 0.0) or 0.0)))
        except Exception:
            trim_db = 0.0

        gain = (global_volume / 100.0) * (slot_volume / 100.0) * auto_gain
        gain *= 10.0 ** (trim_db / 20.0)
        gain = max(0.0, min(3.0, gain))

        command = (
            "ffmpeg -v error -nostdin -i "
            + shlex.quote(str(path))
            + f" -filter:a volume={gain:.4f} "
            + " -f f32le -ac 2 -ar 48000 pipe:1 | "
            + f"pacat --playback --device={SOUNDBOARD_BUS} "
            + "--raw --format=float32le --rate=48000 --channels=2 "
            + "--volume=65536 --latency-msec=42 --process-time-msec=12 "
            + f"--property=media.name={SOUNDBOARD_PLAYER_MEDIA_NAME}"
        )

        try:
            process = self._popen(
                ["bash", "-lc", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            return False

        with self._lock:
            self._cleanup_players_unlocked()
            self._players.append(process)
        return True

    def _cleanup_players_unlocked(self) -> None:
        active: list[subprocess.Popen] = []
        for process in self._players:
            try:
                if process.poll() is None:
                    active.append(process)
            except Exception:
                pass
        self._players = active

    def _sink_input_ids(self) -> list[str]:
        try:
            result = self._run(
                ["pactl", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
        except Exception:
            return []
        if getattr(result, "returncode", 1) != 0:
            return []

        ids: list[str] = []
        current_id = ""
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_id, current_lines
            if current_id and f'media.name = "{SOUNDBOARD_PLAYER_MEDIA_NAME}"' in "\n".join(current_lines):
                ids.append(current_id)
            current_id = ""
            current_lines = []

        for raw in str(getattr(result, "stdout", "")).splitlines():
            line = raw.rstrip()
            match = re.match(r"Sink Input #(\d+)", line)
            if match:
                flush()
                current_id = match.group(1)
                current_lines = [line]
            elif current_id:
                current_lines.append(line)
        flush()
        return ids

    def _terminate_process(self, process: subprocess.Popen) -> bool:
        try:
            if process.poll() is not None:
                return False
        except Exception:
            return False

        try:
            try:
                self._killpg(int(process.pid), signal.SIGTERM)
            except Exception:
                process.terminate()

            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                try:
                    self._killpg(int(process.pid), signal.SIGKILL)
                except Exception:
                    process.kill()
                try:
                    process.wait(timeout=0.25)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def stop_all(self) -> int:
        stream_ids = self._sink_input_ids()
        for volume in (70, 40, 15, 0):
            for stream_id in stream_ids:
                try:
                    self._run(
                        ["pactl", "set-sink-input-volume", stream_id, f"{volume}%"],
                        capture_output=True,
                        text=True,
                        timeout=0.4,
                    )
                except Exception:
                    pass
            if stream_ids:
                self._sleep(0.015)

        with self._lock:
            players = list(self._players)
            self._players = []

        stopped = sum(1 for process in players if self._terminate_process(process))

        remaining_ids = self._sink_input_ids()
        for stream_id in dict.fromkeys(stream_ids + remaining_ids):
            try:
                result = self._run(
                    ["pactl", "kill-sink-input", stream_id],
                    capture_output=True,
                    text=True,
                    timeout=0.8,
                )
                if getattr(result, "returncode", 1) == 0:
                    stopped += 1
            except Exception:
                pass
        return stopped

    def shutdown(self) -> None:
        self.stop_all()
