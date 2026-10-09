from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ...models import EqProfile
from ...audio_contract import AudioLayout, STEREO
from ...audio_safety import (
    FINITE_GUARD_ABI,
    finite_guard_inputs,
    finite_guard_links_to,
    render_finite_guard_nodes,
)


@dataclass
class EqRuntimeSlot:
    """Runtime state for one logical playback EQ lane."""

    key: str
    logical_sink: str
    proc: subprocess.Popen | None = None
    signature: str = ""
    status: str = "idle"


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def stereo_width_matrix(stereo_width: int) -> tuple[float, float]:
    """Return same-channel and cross-channel gains for a 0..100 width."""

    width = _clamp_int(stereo_width, 0, 100) / 100.0
    return 0.5 * (1.0 + width), 0.5 * (1.0 - width)


def build_eq_slot_signature(
    *,
    key: str,
    profile: EqProfile,
    target_label: str,
    target_sink: str,
    stereo_width: int,
    layout: AudioLayout = STEREO,
    finite_guard_active: bool = False,
) -> str:
    return json.dumps(
        {
            "key": key,
            "profile": profile.to_dict(),
            "target_label": target_label,
            "target_sink": target_sink,
            "stereo_width": _clamp_int(stereo_width, 0, 100),
            "audio_layout": {
                "sample_rate": layout.sample_rate,
                "positions": list(layout.positions),
            },
            "finite_guard": {
                "active": bool(finite_guard_active),
                "abi": FINITE_GUARD_ABI if finite_guard_active else 0,
            },
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def build_eq_slot_status(*, profile_name: str, target_label: str, muted: bool, volume: int) -> str:
    extra: list[str] = []
    if muted:
        extra.append("muted")
    elif volume != 100:
        extra.append(f"vol {volume}%")
    suffix = f" • {', '.join(extra)}" if extra else ""
    return f"active ({profile_name} → {target_label}){suffix}"


def render_filters(profile: EqProfile) -> str:
    lines = []
    for band in profile.bands:
        lines.append(
            f'{{ type = bq_peaking freq = {band.frequency:.1f} gain = {band.gain_db:.2f} q = {band.q:.3f} }}'
        )
    return "\n          ".join(lines)


def render_eq_dropin(
    *,
    key: str,
    logical_sink: str,
    profile: EqProfile,
    target_sink: str,
    stereo_width: int,
    layout: AudioLayout = STEREO,
    finite_guard_active: bool = False,
) -> str:
    # The current EQ/width module is intentionally stereo.  The layout is
    # explicit now so core safety graph construction is not hard-coded to L/R;
    # a future optional multichannel module can own its own DSP/downmix graph.
    if layout.channels != 2:
        raise ValueError("The current K-Sounds EQ runtime supports stereo layouts only")

    filters = render_filters(profile)
    title = key.upper()
    same_gain, cross_gain = stereo_width_matrix(stereo_width)
    positions = layout.pipewire_positions

    guard_nodes = ""
    guard_links = ""
    graph_inputs = '"eq:In 1"  "eq:In 2"'
    if finite_guard_active:
        guard_nodes = "\n" + render_finite_guard_nodes(layout)
        guard_links = "\n" + finite_guard_links_to(layout=layout, downstream_node="eq")
        graph_inputs = finite_guard_inputs(layout)

    return f'''context.modules = [
  {{
    name = libpipewire-module-filter-chain
    args = {{
      node.description = "K-Sound Hub {title} EQ"
      media.name = "K-Sound Hub {title} EQ"
      filter.graph = {{
        nodes = [{guard_nodes}
          {{
            type = builtin
            name = eq
            label = param_eq
            config = {{
              filters = [
          {filters}
              ]
            }}
          }}
          {{
            type = builtin
            name = width_left
            label = mixer
            control = {{
              "Gain 1" = {same_gain:.6f}
              "Gain 2" = {cross_gain:.6f}
            }}
          }}
          {{
            type = builtin
            name = width_right
            label = mixer
            control = {{
              "Gain 1" = {same_gain:.6f}
              "Gain 2" = {cross_gain:.6f}
            }}
          }}
        ]
        links = [{guard_links}
          {{ output = "eq:Out 1" input = "width_left:In 1" }}
          {{ output = "eq:Out 2" input = "width_left:In 2" }}
          {{ output = "eq:Out 2" input = "width_right:In 1" }}
          {{ output = "eq:Out 1" input = "width_right:In 2" }}
        ]
        inputs  = [ {graph_inputs} ]
        outputs = [ "width_left:Out" "width_right:Out" ]
      }}
      capture.props = {{
        node.name = "ksh_{key}_eq.capture"
        target.object = "{logical_sink}"
        stream.capture.sink = true
        node.passive = true
        node.dont-reconnect = true
        audio.channels = {layout.channels}
        audio.position = {positions}
      }}
      playback.props = {{
        node.name = "ksh_{key}_eq.playback"
        target.object = "{target_sink}"
        node.passive = true
        node.dont-reconnect = true
        audio.channels = {layout.channels}
        audio.position = {positions}
      }}
    }}
  }}
]
'''
