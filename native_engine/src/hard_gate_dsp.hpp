#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

namespace ksound::hard_gate {

struct Settings {
    bool enabled{false};
    float threshold_percent{12.0f};
    float volume_percent{100.0f};
};

class Processor {
public:
    void reset() noexcept {
        open_ = true;
        hold_chunks_ = 0;
        gain_ = 1.0f;
        last_peak_ = 0.0f;
    }

    void process(std::vector<float>& frames, const Settings& settings) noexcept {
        if (frames.empty()) {
            return;
        }

        if (!settings.enabled) {
            open_ = true;
            hold_chunks_ = 0;
            gain_ = 1.0f;
            return;
        }

        const float peak = chunk_peak(frames, settings.volume_percent);
        last_peak_ = peak;

        const float open_threshold = threshold_to_peak(settings.threshold_percent);
        const float close_threshold = std::max(0.00035f, open_threshold * 0.50f);

        if (peak >= open_threshold) {
            open_ = true;
            hold_chunks_ = 18; // 180 ms at 10 ms chunks, prevents chopped word endings.
        } else if (open_ && peak < close_threshold) {
            if (hold_chunks_ > 0) {
                --hold_chunks_;
            } else {
                open_ = false;
            }
        }

        const float start_gain = clamp(gain_, 0.0f, 1.0f);
        const float target_gain = open_ ? 1.0f : 0.0f;
        constexpr int channels = 2;
        const int frames_count = std::max(1, static_cast<int>(frames.size()) / channels);

        // Sample-level de-clicking:
        // - fast 2 ms attack to avoid eating words;
        // - slower 6 ms release to avoid clicks/crackles on close.
        const int ramp_frames = target_gain > start_gain ? 96 : 288;

        for (int frame = 0; frame < frames_count; ++frame) {
            const float progress = std::min(
                1.0f,
                static_cast<float>(frame + 1) / static_cast<float>(ramp_frames)
            );
            const float gain = start_gain + ((target_gain - start_gain) * progress);

            for (int ch = 0; ch < channels; ++ch) {
                const int index = frame * channels + ch;
                float& sample = frames[static_cast<std::size_t>(index)];
                sample = std::isfinite(sample) ? sample * gain : 0.0f;
            }
        }

        gain_ = target_gain;
    }

    [[nodiscard]] float last_peak() const noexcept { return last_peak_; }

private:
    static float clamp(float value, float low, float high) noexcept {
        return std::max(low, std::min(high, value));
    }

    static float threshold_to_peak(float threshold_percent) noexcept {
        const float normalized = clamp(threshold_percent, 0.0f, 100.0f) / 100.0f;

        // User slider -> audio peak threshold.
        // This intentionally gives the upper half of the slider real authority:
        // 0%  ~= tiny room noise
        // 25% ~= light gate
        // 50% ~= normal voice gate
        // 75%+ strong gate
        return 0.0012f + std::pow(normalized, 1.35f) * 0.42f;
    }

    static float chunk_peak(const std::vector<float>& frames, float volume_percent) noexcept {
        float raw_peak = 0.0f;
        for (float sample : frames) {
            if (std::isfinite(sample)) {
                raw_peak = std::max(raw_peak, std::fabs(sample));
            }
        }

        const float volume_scalar = clamp(volume_percent / 100.0f, 0.0f, 1.5f);
        return raw_peak * volume_scalar;
    }

    bool open_{true};
    int hold_chunks_{0};
    float gain_{1.0f};
    float last_peak_{0.0f};
};

} // namespace ksound::hard_gate
