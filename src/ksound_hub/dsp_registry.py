from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class DspParameterSpec:
    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    step: float
    unit: str = ""

    def normalize(self, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(self.default)
        if not math.isfinite(numeric):
            numeric = float(self.default)
        numeric = max(float(self.minimum), min(float(self.maximum), numeric))
        if self.step > 0:
            steps = round((numeric - self.minimum) / self.step)
            numeric = self.minimum + steps * self.step
            numeric = max(float(self.minimum), min(float(self.maximum), numeric))
        return float(numeric)


@dataclass(frozen=True)
class DspModuleSpec:
    module_type: str
    name: str
    description: str
    allowed_targets: tuple[str, ...]
    runtime_targets: tuple[str, ...]
    parameters: tuple[DspParameterSpec, ...]
    max_instances: int = 1
    exclusive_group: str = ""
    portable: bool = True

    @property
    def lab_ready(self) -> bool:
        return "vfx" in self.runtime_targets


class DspModuleRegistry:
    """Declarative capability registry shared by presets and the temporary lab.

    allowed_targets describes where a module conceptually belongs. runtime_targets
    describes targets that the current Core can actually execute today. Keeping
    both separate lets the preset format stay stable while runtime coverage grows.
    """

    def __init__(self, specs: list[DspModuleSpec] | tuple[DspModuleSpec, ...]):
        self._specs = {spec.module_type: spec for spec in specs}

    @classmethod
    def default(cls) -> "DspModuleRegistry":
        common = ("input", "output", "vfx")
        return cls(
            [
                DspModuleSpec(
                    "gain",
                    "Gain",
                    "Simple level trim in dB.",
                    common,
                    ("vfx",),
                    (DspParameterSpec("gain_db", "Gain", 0.0, -24.0, 24.0, 0.5, "dB"),),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "highpass",
                    "High-pass",
                    "Remove low frequencies below the cutoff.",
                    common,
                    ("vfx",),
                    (DspParameterSpec("frequency_hz", "Cutoff", 120.0, 40.0, 18000.0, 10.0, "Hz"),),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "lowpass",
                    "Low-pass",
                    "Remove high frequencies above the cutoff.",
                    common,
                    ("vfx",),
                    (DspParameterSpec("frequency_hz", "Cutoff", 7000.0, 40.0, 18000.0, 10.0, "Hz"),),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "saturation",
                    "Saturation",
                    "Soft nonlinear drive for colour and density.",
                    common,
                    ("vfx",),
                    (DspParameterSpec("drive", "Drive", 1.8, 1.0, 10.0, 0.1, "x"),),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "delay",
                    "Delay",
                    "Short/medium feedback delay with its own wet mix.",
                    ("output", "vfx"),
                    ("vfx",),
                    (
                        DspParameterSpec("delay_ms", "Delay", 120.0, 10.0, 500.0, 1.0, "ms"),
                        DspParameterSpec("feedback_percent", "Feedback", 25.0, 0.0, 85.0, 1.0, "%"),
                        DspParameterSpec("mix_percent", "Mix", 30.0, 0.0, 100.0, 1.0, "%"),
                    ),
                    max_instances=2,
                ),
                # IP14m: genuinely live, ordered Custom/Preset Lab DSP blocks.
                # The three float slots in the native state protocol are sufficient
                # for all current parameters (no silent placeholders in the Lab).
                DspModuleSpec(
                    "distortion",
                    "Distortion",
                    "Harder clipping colour with independent dry/wet mix.",
                    ("vfx",),
                    ("vfx",),
                    (
                        DspParameterSpec("drive", "Drive", 6.0, 1.0, 20.0, 0.1, "x"),
                        DspParameterSpec("mix_percent", "Mix", 75.0, 0.0, 100.0, 1.0, "%"),
                    ),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "pitch",
                    "Pitch",
                    "Creative time-domain pitch shift; formants are not preserved.",
                    ("vfx",),
                    ("vfx",),
                    (
                        DspParameterSpec("semitones", "Pitch", -5.0, -12.0, 12.0, 0.5, "st"),
                        DspParameterSpec("mix_percent", "Mix", 100.0, 0.0, 100.0, 1.0, "%"),
                    ),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "reverb",
                    "Reverb",
                    "Small/large room tail with controllable pre-delay and decay.",
                    ("vfx",),
                    ("vfx",),
                    (
                        DspParameterSpec("predelay_ms", "Pre-delay", 22.0, 0.0, 100.0, 1.0, "ms"),
                        DspParameterSpec("decay_percent", "Decay", 50.0, 0.0, 100.0, 1.0, "%"),
                        DspParameterSpec("mix_percent", "Mix", 35.0, 0.0, 100.0, 1.0, "%"),
                    ),
                    max_instances=2,
                ),
                # Registry-only declarations for existing K-Sounds processors.
                # They are intentionally not exposed in the live Lab until the
                # generic chain runtime can execute them without translating to
                # legacy hard-coded channel fields.
                DspModuleSpec(
                    "parametric_eq",
                    "Parametric EQ",
                    "Multi-band parametric equalizer.",
                    common,
                    (),
                    (),
                    max_instances=4,
                ),
                DspModuleSpec(
                    "compressor",
                    "Compressor",
                    "Dynamic range compressor.",
                    common,
                    (),
                    (),
                    max_instances=2,
                ),
                DspModuleSpec(
                    "limiter",
                    "Limiter",
                    "Peak limiter / output safety stage.",
                    ("input", "output"),
                    (),
                    (),
                    max_instances=1,
                    exclusive_group="final_limiter",
                ),
                DspModuleSpec(
                    "deesser",
                    "De-esser",
                    "Reduce excessive sibilance in voice.",
                    ("input", "vfx"),
                    (),
                    (),
                    max_instances=1,
                ),
                DspModuleSpec(
                    "hard_gate",
                    "Hard Gate",
                    "Hard noise gate for MICRO input.",
                    ("input",),
                    (),
                    (),
                    max_instances=1,
                    exclusive_group="input_gate",
                ),
            ]
        )

    def get(self, module_type: str) -> DspModuleSpec | None:
        return self._specs.get(str(module_type or "").strip().lower())

    def all(self) -> tuple[DspModuleSpec, ...]:
        return tuple(self._specs.values())

    def for_target(self, target: str, *, runtime_only: bool = False) -> tuple[DspModuleSpec, ...]:
        wanted = str(target or "").strip().lower()
        items: list[DspModuleSpec] = []
        for spec in self._specs.values():
            allowed = spec.runtime_targets if runtime_only else spec.allowed_targets
            if wanted in allowed:
                items.append(spec)
        return tuple(items)
