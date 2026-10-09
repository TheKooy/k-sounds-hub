from __future__ import annotations

import math
from pathlib import Path

from .audio_contract import AudioLayout


FINITE_GUARD_ABI = 1
FINITE_GUARD_PLUGIN_BASENAME = "ksounds_finite_guard"
FINITE_GUARD_PLUGIN_LABEL = "ksounds_finite_guard_mono"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def finite_guard_plugin_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else project_root()
    return base / "native_engine" / "build" / f"{FINITE_GUARD_PLUGIN_BASENAME}.so"


def finite_guard_available(root: Path | None = None) -> bool:
    return finite_guard_plugin_path(root).is_file()


def finite_or_silence(sample: float) -> float:
    """Reference safety policy: preserve every finite value, reject only non-finite PCM."""

    return sample if math.isfinite(sample) else 0.0


def render_finite_guard_nodes(layout: AudioLayout) -> str:
    chunks: list[str] = []
    for index in range(layout.channels):
        chunks.append(
            f'''          {{
            type = ladspa
            name = finite_guard_{index}
            plugin = {FINITE_GUARD_PLUGIN_BASENAME}
            label = {FINITE_GUARD_PLUGIN_LABEL}
          }}'''
        )
    return "\n".join(chunks)


def finite_guard_inputs(layout: AudioLayout) -> str:
    return " ".join(f'"finite_guard_{index}:Input"' for index in range(layout.channels))


def finite_guard_links_to(*, layout: AudioLayout, downstream_node: str, input_prefix: str = "In") -> str:
    return "\n".join(
        f'          {{ output = "finite_guard_{index}:Output" input = "{downstream_node}:{input_prefix} {index + 1}" }}'
        for index in range(layout.channels)
    )
