from __future__ import annotations

import re
from dataclasses import dataclass


_POSITION_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class AudioLayout:
    """Explicit PipeWire channel layout used at K-Sounds processing boundaries.

    The core currently instantiates only ``STEREO``.  Keeping channel positions
    explicit makes safety/DSP graph construction channel-count agnostic so an
    optional multichannel module can supply another layout later without
    changing the core safety contract.
    """

    positions: tuple[str, ...]
    sample_rate: int = 48_000

    def __post_init__(self) -> None:
        positions = tuple(str(position).strip() for position in self.positions)
        if not positions:
            raise ValueError("AudioLayout requires at least one channel position")
        if len(set(positions)) != len(positions):
            raise ValueError("AudioLayout channel positions must be unique")
        if any(_POSITION_RE.fullmatch(position) is None for position in positions):
            raise ValueError("AudioLayout contains an invalid PipeWire channel position")
        if int(self.sample_rate) <= 0:
            raise ValueError("AudioLayout sample_rate must be positive")
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "sample_rate", int(self.sample_rate))

    @property
    def channels(self) -> int:
        return len(self.positions)

    @property
    def pipewire_positions(self) -> str:
        return "[ " + " ".join(self.positions) + " ]"


STEREO = AudioLayout(("FL", "FR"))
