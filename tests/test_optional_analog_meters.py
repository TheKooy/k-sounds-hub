from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_APP = ROOT / "src" / "ksound_hub" / "glass_app.py"
PIPEWIRE = ROOT / "src" / "ksound_hub" / "audio" / "pipewire.py"


def _section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_meter_uses_native_amplitude_without_hidden_calibration() -> None:
    source = GLASS_APP.read_text(encoding="utf-8")
    controller = _section(source, "    def meter_levels(self, channel_key: str)", "    def list_app_streams")
    animation = _section(source, "    def _animate_meters(self)", "    def _save_geometry_now")

    assert "GLASS_METER_INPUT_SCALE" not in source
    assert 'left, right = self.audio_engine.meter_levels(key)' in controller
    assert "GLASS_METER_INPUT_SCALE" not in animation


def test_show_meters_owns_the_optional_module_lifecycle() -> None:
    glass = GLASS_APP.read_text(encoding="utf-8")
    pipewire = PIPEWIRE.read_text(encoding="utf-8")

    controller_toggle = _section(glass, "    def set_metering_enabled(self, enabled: bool)", "    def list_app_streams")
    window_toggle = _section(glass, "    def _set_meters_visible(self, visible: bool)", "    def _apply_visual_setting")
    timer_start = _section(glass, "    def _start_meter_updates(self)", "    def _animate_meters")

    assert "self.audio_engine.stop_meter_probes()" in controller_toggle
    assert "self.backend_controller.set_metering_enabled(enabled)" in window_toggle
    assert "timer.start()" in window_toggle
    assert "timer.stop()" in window_toggle
    assert 'KSH_METER_INTERVAL_MS", "25"' in timer_start
    assert '"--period-ms",\n            "30"' in pipewire


def test_meter_ui_uses_fast_attack_and_slow_release() -> None:
    source = GLASS_APP.read_text(encoding="utf-8")
    meter = _section(source, "class LevelMeter(QWidget):", "class TitleBar(QFrame):")

    assert "ATTACK_BLEND = 0.96" in meter
    assert "RELEASE_BLEND = 0.20" in meter
    assert "self.current += (self.target - self.current) * blend" in meter
    assert "def reset(self) -> None" in meter


def test_meter_ui_maps_sample_peaks_to_a_generic_dbfs_scale() -> None:
    source = GLASS_APP.read_text(encoding="utf-8")
    meter = _section(source, "class LevelMeter(QWidget):", "class TitleBar(QFrame):")

    assert "MIN_DB = -60.0" in meter
    assert "ALIGNMENT_DB = -18.0" in meter
    assert "WARNING_DB = -9.0" in meter
    assert "DANGER_DB = -3.0" in meter
    assert "db = 20.0 * math.log10(raw)" in meter
    assert "ALIGNMENT_VISUAL_LEVEL = (ALIGNMENT_DB - MIN_DB) / -MIN_DB" in meter
    assert "WARNING_VISUAL_LEVEL = (WARNING_DB - MIN_DB) / -MIN_DB" in meter
    assert "DANGER_VISUAL_LEVEL = (DANGER_DB - MIN_DB) / -MIN_DB" in meter
    assert "CLIP_VISUAL_LEVEL" not in meter
    assert "raw = max(0.0, min(1.0, raw))" in meter
    assert "VISUAL_GAIN" not in meter
    assert "VISUAL_GAMMA" not in meter


def test_native_payload_cache_is_frame_bounded_and_rejects_stale_data() -> None:
    source = PIPEWIRE.read_text(encoding="utf-8")
    reader = _section(source, "    def _read_native_meter_levels_payload(self)", "    def _native_meter_levels")

    assert "now - self._native_meter_levels_last_seen_at <= 0.25" in reader
    assert "now - self._native_meter_levels_last_poll_at < 0.02" in reader
    assert "return cache if cache_is_fresh else {}" in reader


if __name__ == "__main__":
    test_meter_uses_native_amplitude_without_hidden_calibration()
    test_show_meters_owns_the_optional_module_lifecycle()
    test_meter_ui_uses_fast_attack_and_slow_release()
    test_meter_ui_maps_sample_peaks_to_a_generic_dbfs_scale()
    test_native_payload_cache_is_frame_bounded_and_rejects_stale_data()
    print("Optional analog meter module contracts: passed")
