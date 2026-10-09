"""IP14n6: a muted MIC OUT slider MUST set the eventual headphone level."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
GLASS = (ROOT / "src/ksound_hub/glass_app.py").read_text(encoding="utf-8")
AUDIO = (ROOT / "src/ksound_hub/audio/pipewire_v2_final.py").read_text(encoding="utf-8")


def method(name: str, source: str = GLASS):
    found = [n for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == (2 if name == "_set_mixer_shortcut_volume_target" else 1), (name, len(found))
    namespace = {}
    # The first shortcut method belongs to GlassBackendController. A second
    # wrapper of the same name belongs to the main window.
    exec(compile(ast.Module(body=[found[0]], type_ignores=[]), "isolated-ip14n6", "exec"),
         {"ChannelConfig":type("ChannelConfig", (), {})}, namespace)
    return namespace[name]


def fake_ui(key="return-mic", volume=10, muted=True):
    channel = SimpleNamespace(key=key, volume=volume, muted=muted)
    signals=[]
    queued=[]
    saved=[]
    ui = SimpleNamespace(
        _normalize_channel_key=lambda k:k,
        _find_channel=lambda k:channel if k == key else None,
        _overlay_text=lambda c,k: k,
        overlay_message_requested=SimpleNamespace(emit=lambda *v: signals.append(v)),
        channel_state_changed=SimpleNamespace(emit=lambda *v: signals.append(v)),
        _queue_realtime_volume_apply=lambda *v,**kw:queued.append((v,kw)),
        _save_timer=SimpleNamespace(start=lambda:saved.append(channel.volume)),
        _pending_apply_keys=set(),
        _apply_timer=SimpleNamespace(isActive=lambda:True),
    )
    return ui,channel,queued,saved


def test_muted_micout_slider_commits_five_percent_and_keeps_mute():
    ui,ch,queued,saved=fake_ui()
    assert method("_apply_mixer_action")(ui, "return-mic", "set-volume", volume=5)
    assert ch.volume == 5 and ch.muted is True
    assert queued == [(("return-mic",), {})]
    assert saved == [5]


def test_other_muted_channels_keep_historical_volume_shortcut_behavior():
    ui,ch,queued,saved=fake_ui(key="game")
    assert method("_apply_mixer_action")(ui,"game","set-volume",volume=5)
    assert ch.volume == 10 and ch.muted is True
    assert queued == [] and saved == []


def test_micout_shortcuts_can_lower_safe_volume_without_unmuting():
    ui,ch,queued,saved=fake_ui()
    assert method("_apply_mixer_action")(ui,"return-mic","voldown")
    assert ch.volume == 5 and ch.muted is True and queued and saved == [5]
    assert method("_apply_mixer_action")(ui,"return-mic","volup")
    assert ch.volume == 10 and ch.muted is True


def test_micout_live_drag_queues_engine_even_while_muted():
    ui,ch,queued,_=fake_ui()
    assert method("set_channel_volume_live")(ui,"return-mic",5)
    assert ch.volume == 5 and ch.muted is True
    assert queued == [(("return-mic",), {})]


def test_other_muted_channel_live_drag_does_not_apply_audio():
    ui,ch,queued,_=fake_ui(key="game")
    assert method("set_channel_volume_live")(ui,"game",5)
    assert ch.volume == 5 and ch.muted is True
    assert queued == []


def test_shortcut_target_for_muted_micout_is_queued_for_safe_apply():
    ui,ch,queued,saved=fake_ui()
    assert method("_set_mixer_shortcut_volume_target")(ui,"return-mic",5,final=True)
    assert ch.volume == 5 and ch.muted is True
    assert queued == [(("return-mic",), {"final":True})]
    assert saved == [5]


def test_shortcut_target_other_muted_channels_unchanged():
    ui,ch,queued,saved=fake_ui(key="game")
    assert method("_set_mixer_shortcut_volume_target")(ui,"game",5,final=True)
    assert ch.volume == 10 and ch.muted is True
    assert queued == [] and saved == []


def test_real_fallback_control_function_accepts_muted_five_percent():
    # The actual IP14n5 PipeWire handler already understands a muted 5%.
    # Regression here is the UI -> final-volume bridge, not the DSP.
    ch=SimpleNamespace(volume=5, muted=True, enabled=True)
    calls=[]
    class Stub:
        _return_mic_final_muted=True
        _return_mic_final_playback_ids_seen=()
        def _return_mic_final_playback_ids(self):return ["7301"]
        def _run(self,args):
            calls.append(tuple(args))
            return SimpleNamespace(returncode=0)
    stub=Stub()
    handler=method("_apply_return_mic_final_output_controls", AUDIO)
    assert handler(stub,ch)
    assert ("pactl","set-sink-input-mute","7301","1") in calls
    assert ("pactl","set-sink-input-volume","7301","5%") in calls
    assert all(c[1] != "set-sink-input-mute" or c[-1] != "0" for c in calls)
