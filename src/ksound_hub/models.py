from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

from .config import DEFAULT_CHANNELS, DEFAULT_EQ_BANDS


@dataclass
class EqBand:
    frequency: float
    gain_db: float
    q: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EqBand":
        return cls(
            frequency=float(data.get("frequency", 1000.0)),
            gain_db=float(data.get("gain_db", 0.0)),
            q=float(data.get("q", 1.0)),
        )


def _default_eq_band_frequencies() -> list[float]:
    return [float(item["frequency"]) for item in DEFAULT_EQ_BANDS]


def _interpolated_gain_db(source_bands: list[EqBand], target_frequency: float) -> float:
    """Map old presets to the current default EQ frequencies without losing their shape."""

    valid = sorted(
        (
            band
            for band in source_bands
            if float(band.frequency) > 0.0
        ),
        key=lambda band: float(band.frequency),
    )
    if not valid:
        return 0.0

    if len(valid) == 1:
        return round(float(valid[0].gain_db) * 2.0) / 2.0

    target_log = math.log(max(1.0, float(target_frequency)))
    points = [(math.log(max(1.0, float(band.frequency))), float(band.gain_db)) for band in valid]

    if target_log <= points[0][0]:
        return round(points[0][1] * 2.0) / 2.0
    if target_log >= points[-1][0]:
        return round(points[-1][1] * 2.0) / 2.0

    for (f1, g1), (f2, g2) in zip(points, points[1:], strict=False):
        if f1 <= target_log <= f2:
            span = max(0.000001, f2 - f1)
            ratio = (target_log - f1) / span
            gain = g1 + (g2 - g1) * ratio
            return round(gain * 2.0) / 2.0

    nearest = min(valid, key=lambda band: abs(math.log(float(band.frequency)) - target_log))
    return round(float(nearest.gain_db) * 2.0) / 2.0


def _normalized_eq_bands_for_load(source_bands: list[EqBand]) -> list[EqBand]:
    """Keep custom 10-band profiles intact; migrate old/short profiles to 10 bands."""

    if len(source_bands) == len(DEFAULT_EQ_BANDS):
        return [
            EqBand(
                frequency=max(20.0, min(20000.0, float(band.frequency))),
                gain_db=round(max(-12.0, min(12.0, float(band.gain_db))) * 2.0) / 2.0,
                q=max(0.1, min(10.0, float(band.q))),
            )
            for band in source_bands
        ]

    target_frequencies = _default_eq_band_frequencies()
    default_q = float(DEFAULT_EQ_BANDS[0].get("q", 1.0)) if DEFAULT_EQ_BANDS else 1.0
    return [
        EqBand(
            frequency=frequency,
            gain_db=_interpolated_gain_db(source_bands, frequency),
            q=default_q,
        )
        for frequency in target_frequencies
    ]


@dataclass
class EqProfile:
    name: str = "Default"
    bands: list[EqBand] = field(default_factory=list)

    @classmethod
    def default(cls, name: str = "Default") -> "EqProfile":
        return cls(name=name, bands=[EqBand.from_dict(item) for item in DEFAULT_EQ_BANDS])

    @classmethod
    def from_dict(cls, data: dict) -> "EqProfile":
        name = str(data.get("name", "Default"))
        bands = data.get("bands", [])
        if not isinstance(bands, list):
            bands = []

        parsed_bands = [EqBand.from_dict(item) for item in bands]
        if not parsed_bands:
            parsed_bands = [EqBand.from_dict(item) for item in DEFAULT_EQ_BANDS]

        return cls(
            name=name,
            bands=_normalized_eq_bands_for_load(parsed_bands),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "bands": [asdict(band) for band in self.bands]}


@dataclass
class ChannelConfig:
    key: str
    name: str
    enabled: bool = True
    kind: str = "playback"
    volume: int = 100
    muted: bool = False
    hard_gate_enabled: bool = False
    hard_gate_threshold: int = 12
    # Native MICRO processing. The master switch bypasses every added DSP
    # stage while leaving the existing Hard Gate independent. Default OFF
    # preserves the pre-IP11 sonic path until the user explicitly tests it.
    micro_processing_enabled: bool = False
    micro_input_gain_db: float = 0.0
    micro_noise_suppression_enabled: bool = False
    micro_noise_suppression: int = 0
    # Smooth Tune Gate. Unlike Hard Gate this is a gentle downward gate/expander
    # and follows the Processing master bypass.
    micro_gate_enabled: bool = False
    micro_gate_threshold: int = 35
    micro_compressor_enabled: bool = False
    micro_compressor_amount: int = 45
    micro_deesser_enabled: bool = False
    micro_deesser_amount: int = 35
    micro_limiter_enabled: bool = True
    micro_limiter_ceiling_db: float = -1.0
    # Native low-latency Voice FX are independent from TUNE/Processing.
    # The effect is applied only to the physical MICRO input; Soundboard and
    # injected channels keep their original audio.
    # IP13b: multiple native Voice FX may be active at once. The legacy
    # enabled/preset fields stay mirrored for backward compatibility with IP13a.
    micro_voice_fx_enabled: bool = False
    micro_voice_fx_preset: str = "radio"
    micro_voice_fx_active: list[str] = field(default_factory=list)
    micro_voice_fx_amount: int = 72
    # Generic portable DSP chain used by the IP14 Preset Lab. The preset is
    # declarative JSON data only; no executable plugin code is stored here.
    micro_custom_fx_enabled: bool = False
    micro_custom_fx_preset: dict[str, Any] = field(default_factory=dict)
    stereo_width: int = 100
    visualizer_enabled: bool = True
    primary_target: str = ""
    linked_channels: list[str] = field(default_factory=list)
    app_rules: list[str] = field(default_factory=list)
    eq_profiles: list[EqProfile] = field(default_factory=lambda: [EqProfile.default()])
    selected_eq_profile: str = "Default"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelConfig":
        profiles_raw = data.get("eq_profiles", [])
        profiles = [EqProfile.from_dict(item) for item in profiles_raw] if isinstance(profiles_raw, list) else []
        if not profiles:
            profiles = [EqProfile.default()]
        selected = str(data.get("selected_eq_profile", profiles[0].name))
        if selected not in {profile.name for profile in profiles}:
            selected = profiles[0].name

        voice_fx_order = ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
        raw_voice_fx_active = data.get("micro_voice_fx_active")
        voice_fx_active: list[str] = []
        if isinstance(raw_voice_fx_active, list):
            requested = {str(item or "").strip().lower() for item in raw_voice_fx_active}
            voice_fx_active = [key for key in voice_fx_order if key in requested]
        else:
            legacy_preset = str(data.get("micro_voice_fx_preset", "radio") or "radio").strip().lower()
            if legacy_preset not in voice_fx_order:
                legacy_preset = "radio"
            if bool(data.get("micro_voice_fx_enabled", False)):
                voice_fx_active = [legacy_preset]

        return cls(
            key=str(data.get("key", "channel")),
            name=str(data.get("name", "Channel")),
            enabled=bool(data.get("enabled", True)),
            kind=str(data.get("kind", "playback")),
            volume=int(data.get("volume", 100)),
            muted=bool(data.get("muted", False)),
            hard_gate_enabled=bool(data.get("hard_gate_enabled", False)),
            hard_gate_threshold=max(0, min(100, int(data.get("hard_gate_threshold", 12)))),
            micro_processing_enabled=bool(data.get("micro_processing_enabled", False)),
            micro_input_gain_db=max(-12.0, min(24.0, float(data.get("micro_input_gain_db", 0.0)))),
            micro_noise_suppression_enabled=bool(data.get("micro_noise_suppression_enabled", int(data.get("micro_noise_suppression", 0)) > 0)),
            micro_noise_suppression=max(0, min(100, int(data.get("micro_noise_suppression", 0)))),
            micro_gate_enabled=bool(data.get("micro_gate_enabled", False)),
            micro_gate_threshold=max(0, min(100, int(data.get("micro_gate_threshold", 35)))),
            micro_compressor_enabled=bool(data.get("micro_compressor_enabled", False)),
            micro_compressor_amount=max(0, min(100, int(data.get("micro_compressor_amount", 45)))),
            micro_deesser_enabled=bool(data.get("micro_deesser_enabled", False)),
            micro_deesser_amount=max(0, min(100, int(data.get("micro_deesser_amount", 35)))),
            micro_limiter_enabled=bool(data.get("micro_limiter_enabled", True)),
            micro_limiter_ceiling_db=max(-12.0, min(0.0, float(data.get("micro_limiter_ceiling_db", -1.0)))),
            micro_voice_fx_enabled=bool(voice_fx_active),
            micro_voice_fx_preset=(voice_fx_active[0] if voice_fx_active else (
                str(data.get("micro_voice_fx_preset", "radio") or "radio").strip().lower()
                if str(data.get("micro_voice_fx_preset", "radio") or "radio").strip().lower()
                in {"distortion", "pitch_down", "pitch_up", "radio", "telephone", "church", "church_pa", "robot", "reverb"}
                else "radio"
            )),
            micro_voice_fx_active=voice_fx_active,
            micro_voice_fx_amount=max(0, min(100, int(data.get("micro_voice_fx_amount", 72)))),
            micro_custom_fx_enabled=bool(data.get("micro_custom_fx_enabled", False)),
            micro_custom_fx_preset=(
                dict(data.get("micro_custom_fx_preset", {}))
                if isinstance(data.get("micro_custom_fx_preset", {}), dict)
                else {}
            ),
            stereo_width=max(0, min(100, int(data.get("stereo_width", 100)))),
            visualizer_enabled=bool(data.get("visualizer_enabled", True)),
            primary_target=str(data.get("primary_target", "")),
            linked_channels=[
                str(item)
                for item in (data.get("linked_channels", []) if isinstance(data.get("linked_channels", []), list) else [])
            ],
            app_rules=[
                str(item)
                for item in (data.get("app_rules", []) if isinstance(data.get("app_rules", []), list) else [])
            ],
            eq_profiles=profiles,
            selected_eq_profile=selected,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "enabled": self.enabled,
            "kind": self.kind,
            "volume": self.volume,
            "muted": self.muted,
            "hard_gate_enabled": bool(self.hard_gate_enabled),
            "hard_gate_threshold": max(0, min(100, int(self.hard_gate_threshold))),
            "micro_processing_enabled": bool(self.micro_processing_enabled),
            "micro_input_gain_db": max(-12.0, min(24.0, float(self.micro_input_gain_db))),
            "micro_noise_suppression_enabled": bool(self.micro_noise_suppression_enabled),
            "micro_noise_suppression": max(0, min(100, int(self.micro_noise_suppression))),
            "micro_gate_enabled": bool(self.micro_gate_enabled),
            "micro_gate_threshold": max(0, min(100, int(self.micro_gate_threshold))),
            "micro_compressor_enabled": bool(self.micro_compressor_enabled),
            "micro_compressor_amount": max(0, min(100, int(self.micro_compressor_amount))),
            "micro_deesser_enabled": bool(self.micro_deesser_enabled),
            "micro_deesser_amount": max(0, min(100, int(self.micro_deesser_amount))),
            "micro_limiter_enabled": bool(self.micro_limiter_enabled),
            "micro_limiter_ceiling_db": max(-12.0, min(0.0, float(self.micro_limiter_ceiling_db))),
            "micro_voice_fx_enabled": bool(self.micro_voice_fx_active),
            "micro_voice_fx_preset": (
                str((self.micro_voice_fx_active[0] if self.micro_voice_fx_active else self.micro_voice_fx_preset) or "radio").strip().lower()
                if str((self.micro_voice_fx_active[0] if self.micro_voice_fx_active else self.micro_voice_fx_preset) or "radio").strip().lower()
                in {"distortion", "pitch_down", "pitch_up", "radio", "telephone", "church", "church_pa", "robot", "reverb"}
                else "radio"
            ),
            "micro_voice_fx_active": [
                key for key in ("distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa")
                if key in {str(item or "").strip().lower() for item in self.micro_voice_fx_active}
            ],
            "micro_voice_fx_amount": max(0, min(100, int(self.micro_voice_fx_amount))),
            "micro_custom_fx_enabled": bool(self.micro_custom_fx_enabled),
            "micro_custom_fx_preset": (
                dict(self.micro_custom_fx_preset)
                if isinstance(self.micro_custom_fx_preset, dict)
                else {}
            ),
            "stereo_width": max(0, min(100, int(self.stereo_width))),
            "visualizer_enabled": self.visualizer_enabled,
            "primary_target": self.primary_target,
            "linked_channels": list(self.linked_channels),
            "app_rules": list(self.app_rules),
            "eq_profiles": [profile.to_dict() for profile in self.eq_profiles],
            "selected_eq_profile": self.selected_eq_profile,
        }


@dataclass
class AppSettings:
    overlay_enabled: bool = False
    visualizer_enabled: bool = True
    close_to_tray: bool = True
    skip_exit_confirmation: bool = False
    wallpaper_enabled: bool = False
    wallpaper_path: str = ""
    wallpaper_blur: int = 0
    wallpaper_tint_strength: int = 40
    glass_background_blur: int = 18
    glass_background_saturation: int = 50
    glass_background_contrast: int = 50
    glass_background_darkness: int = 55
    glass_opacity: int = 70
    auto_sort_app_streams_by_role: bool = True
    micro_processing_ab_shortcut: str = "Ctrl+B"
    micro_processing_ab_shortcut_enabled: bool = True
    shortcut_bindings: list[dict[str, Any]] = field(default_factory=list)
    favorite_output_targets: list[str] = field(default_factory=list)
    favorite_input_targets: list[str] = field(default_factory=list)
    favorite_micro_eq_presets: list[str] = field(default_factory=list)
    favorite_output_eq_presets: list[str] = field(default_factory=list)
    installed_modules: list[str] = field(default_factory=lambda: ["eq", "micro_tune", "hard_gate", "voice_fx", "pads", "remote"])
    enabled_modules: list[str] = field(default_factory=lambda: ["eq", "micro_tune", "hard_gate", "voice_fx", "pads", "remote"])
    glass_micro_injection_channels: list[str] = field(default_factory=list)
    glass_micro_injection_volume: int = 125
    # MIC OUT source is a real user preference, not an auxiliary JSON-only key:
    # normal volume/settings saves must not silently erase this route selection.
    glass_return_mic_source: str = ""
    glass_return_mic_micro_enabled: bool = False  # legacy mirror
    channels: list[ChannelConfig] = field(default_factory=list)

    @classmethod
    def default(cls) -> "AppSettings":
        channels = [
            ChannelConfig(
                key=item["key"],
                name=item["name"],
                enabled=bool(item.get("enabled", True)),
                kind=str(item.get("kind", "playback")),
            )
            for item in DEFAULT_CHANNELS
        ]
        return cls(channels=channels)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        channels_raw = data.get("channels", [])
        channels = [ChannelConfig.from_dict(item) for item in channels_raw] if isinstance(channels_raw, list) else []
        if not channels:
            channels = cls.default().channels

        default_channel_names = {
            str(item["key"]): str(item["name"])
            for item in DEFAULT_CHANNELS
            if "key" in item and "name" in item
        }
        for channel in channels:
            if channel.key in default_channel_names:
                channel.name = default_channel_names[channel.key]

        close_to_tray = data.get("close_to_tray")
        if close_to_tray is None and isinstance(channels_raw, list):
            for item in channels_raw:
                if isinstance(item, dict) and "close_to_tray" in item:
                    close_to_tray = bool(item.get("close_to_tray", True))
                    break
        if close_to_tray is None:
            close_to_tray = True

        allowed_micro_injection = {"all", "game", "chat", "media", "more"}
        raw_micro_injection = data.get("glass_micro_injection_channels", [])
        if not isinstance(raw_micro_injection, list):
            raw_micro_injection = []
        glass_micro_injection_channels = sorted({
            str(item).strip().lower()
            for item in raw_micro_injection
            if str(item).strip().lower() in allowed_micro_injection
        })

        try:
            glass_micro_injection_volume = int(data.get("glass_micro_injection_volume", 125))
        except Exception:
            glass_micro_injection_volume = 125
        glass_micro_injection_volume = max(0, min(200, glass_micro_injection_volume))

        raw_shortcut_bindings = data.get("shortcut_bindings", [])
        shortcut_bindings: list[dict[str, Any]] = []
        if isinstance(raw_shortcut_bindings, list):
            for item in raw_shortcut_bindings:
                if isinstance(item, dict):
                    shortcut_bindings.append(dict(item))

        def _clean_favorite_targets(raw) -> list[str]:
            if not isinstance(raw, list):
                return []
            result: list[str] = []
            seen: set[str] = set()
            for item in raw:
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                result.append(value)
            return result

        favorite_output_targets = _clean_favorite_targets(data.get("favorite_output_targets", []))
        favorite_input_targets = _clean_favorite_targets(data.get("favorite_input_targets", []))
        favorite_micro_eq_presets = _clean_favorite_targets(data.get("favorite_micro_eq_presets", []))
        favorite_output_eq_presets = _clean_favorite_targets(data.get("favorite_output_eq_presets", []))

        module_defaults = ["eq", "micro_tune", "hard_gate", "voice_fx", "pads", "remote"]
        raw_installed_modules = data.get("installed_modules")
        installed_modules = (
            _clean_favorite_targets(raw_installed_modules)
            if isinstance(raw_installed_modules, list)
            else list(module_defaults)
        )
        raw_enabled_modules = data.get("enabled_modules")
        enabled_modules = (
            _clean_favorite_targets(raw_enabled_modules)
            if isinstance(raw_enabled_modules, list)
            else list(module_defaults)
        )

        # IP14k migration: Hard Gate used to be part of Micro Tune/Core and had
        # no independent module preference. Existing settings therefore cannot
        # express an intentional Hard Gate module disable. Add it installed +
        # enabled exactly once when loading a pre-IP14k module list. Once saved,
        # future user disables are preserved normally.
        had_hard_gate_module = "hard_gate" in installed_modules
        if not had_hard_gate_module:
            insert_at = installed_modules.index("micro_tune") + 1 if "micro_tune" in installed_modules else len(installed_modules)
            installed_modules.insert(insert_at, "hard_gate")
            if "hard_gate" not in enabled_modules:
                enabled_modules.append("hard_gate")

        installed_set = set(installed_modules)
        enabled_modules = [item for item in enabled_modules if item in installed_set]

        # IP12f migration: the central registry becomes the source of truth,
        # while the old TUNE keys stay mirrored for backward compatibility.
        if not any(
            str(item.get("shortcut_id", "") or "").strip() == "micro.tune.toggle"
            for item in shortcut_bindings
        ):
            shortcut_bindings.append({
                "shortcut_id": "micro.tune.toggle",
                "description": "Toggle MICRO Processing (TUNE)",
                "trigger": str(data.get("micro_processing_ab_shortcut", "Ctrl+B") or ""),
                "enabled": bool(data.get("micro_processing_ab_shortcut_enabled", True)),
                "module": "micro_processing",
                "allow_hold": False,
            })

        return cls(
            overlay_enabled=bool(data.get("overlay_enabled", False)),
            visualizer_enabled=bool(data.get("visualizer_enabled", True)),
            close_to_tray=bool(close_to_tray),
            skip_exit_confirmation=bool(data.get("skip_exit_confirmation", False)),
            wallpaper_enabled=bool(data.get("wallpaper_enabled", False)),
            wallpaper_path=str(data.get("wallpaper_path", "")),
            wallpaper_blur=max(0, min(32, int(data.get("wallpaper_blur", 0)))),
            wallpaper_tint_strength=max(0, min(100, int(data.get("wallpaper_tint_strength", 40)))),
            glass_background_blur=max(0, min(100, int(data.get("glass_background_blur", 18)))),
            glass_background_saturation=max(0, min(100, int(data.get("glass_background_saturation", 50)))),
            glass_background_contrast=max(0, min(100, int(data.get("glass_background_contrast", 50)))),
            glass_background_darkness=max(0, min(100, int(data.get("glass_background_darkness", 55)))),
            glass_opacity=max(0, min(100, int(data.get("glass_opacity", 70)))),
            auto_sort_app_streams_by_role=bool(data.get("auto_sort_app_streams_by_role", True)),
            micro_processing_ab_shortcut=str(data.get("micro_processing_ab_shortcut", "Ctrl+B") or ""),
            micro_processing_ab_shortcut_enabled=bool(data.get("micro_processing_ab_shortcut_enabled", True)),
            shortcut_bindings=shortcut_bindings,
            favorite_output_targets=favorite_output_targets,
            favorite_input_targets=favorite_input_targets,
            favorite_micro_eq_presets=favorite_micro_eq_presets,
            favorite_output_eq_presets=favorite_output_eq_presets,
            installed_modules=installed_modules,
            enabled_modules=enabled_modules,
            glass_micro_injection_channels=glass_micro_injection_channels,
            glass_micro_injection_volume=glass_micro_injection_volume,
            glass_return_mic_source=str(data.get("glass_return_mic_source") or "").strip(),
            glass_return_mic_micro_enabled=bool(data.get("glass_return_mic_micro_enabled", bool(data.get("glass_return_mic_source")))),
            channels=channels,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlay_enabled": self.overlay_enabled,
            "visualizer_enabled": self.visualizer_enabled,
            "close_to_tray": self.close_to_tray,
            "skip_exit_confirmation": bool(self.skip_exit_confirmation),
            "wallpaper_enabled": self.wallpaper_enabled,
            "wallpaper_path": self.wallpaper_path,
            "wallpaper_blur": self.wallpaper_blur,
            "wallpaper_tint_strength": self.wallpaper_tint_strength,
            "glass_background_blur": self.glass_background_blur,
            "glass_background_saturation": self.glass_background_saturation,
            "glass_background_contrast": self.glass_background_contrast,
            "glass_background_darkness": self.glass_background_darkness,
            "glass_opacity": self.glass_opacity,
            "auto_sort_app_streams_by_role": bool(self.auto_sort_app_streams_by_role),
            "micro_processing_ab_shortcut": str(self.micro_processing_ab_shortcut or ""),
            "micro_processing_ab_shortcut_enabled": bool(self.micro_processing_ab_shortcut_enabled),
            "shortcut_bindings": [dict(item) for item in self.shortcut_bindings if isinstance(item, dict)],
            "favorite_output_targets": list(self.favorite_output_targets),
            "favorite_input_targets": list(self.favorite_input_targets),
            "favorite_micro_eq_presets": list(self.favorite_micro_eq_presets),
            "favorite_output_eq_presets": list(self.favorite_output_eq_presets),
            "installed_modules": list(self.installed_modules),
            "enabled_modules": list(self.enabled_modules),
            "glass_micro_injection_channels": list(self.glass_micro_injection_channels),
            "glass_micro_injection_volume": max(0, min(200, int(self.glass_micro_injection_volume))),
            "glass_return_mic_source": str(self.glass_return_mic_source or "").strip(),
            "glass_return_mic_micro_enabled": bool(self.glass_return_mic_micro_enabled),
            "channels": [channel.to_dict() for channel in self.channels],
        }
