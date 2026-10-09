from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ksound_hub.soundboard_service import SoundboardService


def _write_document(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


class _FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0


def test_state_commands_preserve_remote_metadata(tmp_path: Path) -> None:
    config_path = tmp_path / "soundboard.json"
    _write_document(
        config_path,
        {
            "global_volume": 75,
            "remote_background_path": "/tmp/remote-wallpaper.png",
            "remote_contrast": 118,
            "remote_columns": 2,
            "folders": ["Main", "Memes"],
            "slots": [
                {"id": "sb1", "label": "One", "path": ""},
                {"id": "sb2", "label": "Two", "path": ""},
                {"id": "sb3", "label": "Three", "path": ""},
                {"id": "sb4", "label": "Four", "path": ""},
            ],
        },
    )
    service = SoundboardService(config_path, default_columns=4)

    assert service.set_global_volume(33)
    service.set_auto_level_enabled(True)
    assert service.move_slot_by_key("sb1", "right")
    assert service.reorder_slots_by_ids(["sb4", "sb2"])

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["global_volume"] == 33
    assert saved["auto_level_enabled"] is True
    assert saved["remote_background_path"] == "/tmp/remote-wallpaper.png"
    assert saved["remote_contrast"] == 118
    assert saved["folders"] == ["Main", "Memes"]
    assert [slot["id"] for slot in saved["slots"]] == ["sb4", "sb2", "sb1", "sb3"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_slot_aliases_and_playback_share_one_player_owner(tmp_path: Path) -> None:
    audio_path = tmp_path / "Alert sound.wav"
    audio_path.write_bytes(b"not decoded by this unit test")
    config_path = tmp_path / "soundboard.json"
    _write_document(
        config_path,
        {
            "global_volume": 50,
            "auto_level_enabled": True,
            "slots": [
                {
                    "id": "sb1",
                    "label": "Air Horn",
                    "path": str(audio_path),
                    "volume": 80,
                    "auto_gain": 1.5,
                    "trim_db": 0,
                }
            ],
        },
    )

    popen_calls: list[tuple[list[str], dict]] = []
    process = _FakeProcess()

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    service = SoundboardService(config_path, popen_factory=fake_popen)

    assert service.slot_for_key(" AIR   HORN ")["id"] == "sb1"
    assert service.slot_for_key("1")["id"] == "sb1"
    assert service.play_slot_by_key("SB1")
    assert len(popen_calls) == 1

    command = popen_calls[0][0][2]
    assert "--device=soundboard" in command
    assert "media.name=K-Sounds-Hub-Soundboard-Player" in command
    assert "volume=0.6000" in command
    assert popen_calls[0][1]["start_new_session"] is True


def test_stop_all_fades_and_terminates_service_players(tmp_path: Path) -> None:
    audio_path = tmp_path / "sound.wav"
    audio_path.write_bytes(b"sample")
    config_path = tmp_path / "soundboard.json"
    _write_document(
        config_path,
        {"slots": [{"id": "sb1", "label": "One", "path": str(audio_path)}]},
    )

    process = _FakeProcess()
    run_calls: list[list[str]] = []
    killpg_calls: list[tuple[int, int]] = []

    def fake_run(args, **kwargs):
        run_calls.append(list(args))
        if args[:3] == ["pactl", "list", "sink-inputs"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Sink Input #77\n"
                    '    media.name = "K-Sounds-Hub-Soundboard-Player"\n'
                ),
            )
        return SimpleNamespace(returncode=0, stdout="")

    def fake_killpg(pid: int, sig: int) -> None:
        killpg_calls.append((pid, sig))

    service = SoundboardService(
        config_path,
        popen_factory=lambda *args, **kwargs: process,
        run_factory=fake_run,
        killpg=fake_killpg,
        sleep=lambda _seconds: None,
    )

    assert service.play_slot_by_key("sb1")
    stopped = service.stop_all()

    assert stopped == 2
    assert killpg_calls == [(process.pid, signal.SIGTERM)]
    fades = [call for call in run_calls if call[:2] == ["pactl", "set-sink-input-volume"]]
    assert [call[-1] for call in fades] == ["70%", "40%", "15%", "0%"]
    assert ["pactl", "kill-sink-input", "77"] in run_calls


def test_glass_uses_non_ui_soundboard_service_contract() -> None:
    glass = (ROOT / "src" / "ksound_hub" / "glass_app.py").read_text(encoding="utf-8")
    service = (ROOT / "src" / "ksound_hub" / "soundboard_service.py").read_text(encoding="utf-8")

    assert ".ui.soundboard_dialog" not in glass
    assert "SoundboardDialog" not in glass
    assert "self._android_soundboard_dialog" not in glass
    assert "self.soundboard_service = SoundboardService()" in glass
    assert "shared_service = getattr(backend_controller, \"soundboard_service\", None)" in glass
    assert "service.play_slot_by_key(slot)" in glass
    assert "service.move_slot_by_key(slot, direction)" in glass
    assert "self.soundboard_service.play_slot(slot)" in glass
    assert "self.soundboard_service.stop_all()" in glass
    assert "_soundboard_bus_player_processes" not in glass
    assert "class SoundboardService:" in service
    assert "PySide6" not in service


def test_remote_directional_move_respects_eleven_column_layout(tmp_path: Path) -> None:
    config_path = tmp_path / "soundboard.json"
    slots = [
        {"id": f"sb{index + 1}", "label": str(index + 1), "path": ""}
        for index in range(12)
    ]
    _write_document(config_path, {"remote_columns": 11, "slots": slots})
    service = SoundboardService(config_path, default_columns=4)

    assert service.move_slot_by_key("sb1", "down")
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["slots"][11]["id"] == "sb1"
