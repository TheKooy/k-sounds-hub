#include "micro_tune_dsp.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <dlfcn.h>
#include <sstream>
#include <string>
#include <utility>

namespace ksound::micro_tune {
namespace {

constexpr int RATE = 48000;
constexpr int CHANNELS = 2;
constexpr int CHUNK_FRAMES = 480;
constexpr int CHUNK_SAMPLES = CHUNK_FRAMES * CHANNELS;

float clampf(float value, float low, float high) {
    return std::max(low, std::min(high, value));
}

struct Biquad {
    float b0{1.0f}, b1{0.0f}, b2{0.0f}, a1{0.0f}, a2{0.0f}, z1{0.0f}, z2{0.0f};
    float process(float input) {
        const float output = (b0 * input) + z1;
        z1 = (b1 * input) - (a1 * output) + z2;
        z2 = (b2 * input) - (a2 * output);
        return std::isfinite(output) ? output : 0.0f;
    }
};

struct ParametricEqState {
    std::array<std::vector<Biquad>, CHANNELS> filters;
    std::string signature;
};

std::string eq_signature(std::span<const EqBandSpec> bands) {
    std::ostringstream out;
    for (const auto& band : bands) out << band.frequency << ':' << band.gain_db << ':' << band.q << ',';
    return out.str();
}

Biquad make_peaking_eq(const EqBandSpec& band) {
    constexpr float pi = 3.14159265358979323846f;
    const float frequency = clampf(band.frequency, 20.0f, std::min(20000.0f, RATE * 0.45f));
    const float gain_db = clampf(band.gain_db, -12.0f, 12.0f);
    const float q = clampf(band.q, 0.1f, 10.0f);
    const float A = std::pow(10.0f, gain_db / 40.0f);
    const float w0 = 2.0f * pi * frequency / static_cast<float>(RATE);
    const float alpha = std::sin(w0) / (2.0f * q);
    const float cos_w0 = std::cos(w0);
    const float a0 = 1.0f + (alpha / A);
    Biquad filter;
    filter.b0 = (1.0f + (alpha * A)) / a0;
    filter.b1 = (-2.0f * cos_w0) / a0;
    filter.b2 = (1.0f - (alpha * A)) / a0;
    filter.a1 = (-2.0f * cos_w0) / a0;
    filter.a2 = (1.0f - (alpha / A)) / a0;
    return filter;
}

void apply_parametric_eq(std::vector<float>& frames, ParametricEqState& dsp, const Settings& state) {
    if (frames.empty() || state.eq_bands.empty()) { dsp = ParametricEqState{}; return; }
    const std::string wanted = eq_signature(state.eq_bands);
    if (wanted != dsp.signature) {
        dsp = ParametricEqState{};
        dsp.signature = wanted;
        for (int ch = 0; ch < CHANNELS; ++ch) {
            for (const auto& band : state.eq_bands) {
                if (std::fabs(band.gain_db) >= 0.01f) dsp.filters[ch].push_back(make_peaking_eq(band));
            }
        }
    }
    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;
    for (int frame = 0; frame < frame_count; ++frame) {
        for (int ch = 0; ch < CHANNELS; ++ch) {
            const int index = frame * CHANNELS + ch;
            float sample = std::isfinite(frames[index]) ? frames[index] : 0.0f;
            for (auto& filter : dsp.filters[ch]) sample = filter.process(sample);
            frames[index] = sample;
        }
    }
}

class RNNoiseBackend {
public:
    RNNoiseBackend() {
        for (const char* candidate : {"librnnoise.so.0", "librnnoise.so"}) {
            library_ = dlopen(candidate, RTLD_NOW | RTLD_LOCAL);
            if (library_ != nullptr) break;
        }
        if (library_ == nullptr) return;
        create_ = reinterpret_cast<CreateFn>(dlsym(library_, "rnnoise_create"));
        destroy_ = reinterpret_cast<DestroyFn>(dlsym(library_, "rnnoise_destroy"));
        process_ = reinterpret_cast<ProcessFn>(dlsym(library_, "rnnoise_process_frame"));
        if (create_ == nullptr || destroy_ == nullptr || process_ == nullptr) {
            dlclose(library_); library_ = nullptr; create_ = nullptr; destroy_ = nullptr; process_ = nullptr;
        }
    }
    ~RNNoiseBackend() { reset_states(false); if (library_ != nullptr) dlclose(library_); }
    RNNoiseBackend(const RNNoiseBackend&) = delete;
    RNNoiseBackend& operator=(const RNNoiseBackend&) = delete;
    bool available() const { return library_ != nullptr && create_ != nullptr && destroy_ != nullptr && process_ != nullptr; }
    void reset_states(bool recreate = true) {
        if (destroy_ != nullptr) for (void*& state : states_) if (state != nullptr) { destroy_(state); state = nullptr; }
        for (auto& delay_frame : dry_delay_) {
            for (auto& channel : delay_frame) channel.fill(0.0f);
        }
        dry_delay_slot_ = 0;
        if (recreate && available()) ensure_states();
    }
    bool process(std::vector<float>& frames, float strength_percent) {
        const float wet = clampf(strength_percent, 0.0f, 100.0f) / 100.0f;
        if (wet <= 0.0f || frames.size() != static_cast<std::size_t>(CHUNK_SAMPLES) || !ensure_states()) return false;

        std::array<std::array<float, CHUNK_FRAMES>, CHANNELS> input{};
        std::array<std::array<float, CHUNK_FRAMES>, CHANNELS> output{};
        auto& aligned_dry = dry_delay_[dry_delay_slot_];

        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const float sample = std::isfinite(frames[frame * CHANNELS + ch])
                    ? frames[frame * CHANNELS + ch]
                    : 0.0f;
                input[ch][frame] = sample * 32768.0f;
            }
        }

        for (int ch = 0; ch < CHANNELS; ++ch) {
            process_(states_[ch], output[ch].data(), input[ch].data());
        }

        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                const float dry = aligned_dry[ch][frame];
                const float denoised = std::isfinite(output[ch][frame])
                    ? output[ch][frame] / 32768.0f
                    : 0.0f;

                // RNNoise itself is delayed. Mixing it with the current dry
                // frame creates the audible doubled/comb-filtered voice seen
                // at partial Strength values. Mix against a two-frame delayed
                // dry reference instead. 100% remains untouched RNNoise.
                frames[index] = wet >= 0.9999f
                    ? denoised
                    : dry + ((denoised - dry) * wet);
            }
        }

        // Replace the slot only after it has been consumed as the aligned dry
        // reference. Two slots = 960 samples = ~20 ms at 48 kHz.
        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                aligned_dry[ch][frame] = input[ch][frame] / 32768.0f;
            }
        }
        dry_delay_slot_ = (dry_delay_slot_ + 1) % static_cast<int>(dry_delay_.size());
        return true;
    }
private:
    using CreateFn = void* (*)(void*);
    using DestroyFn = void (*)(void*);
    using ProcessFn = float (*)(void*, float*, const float*);
    bool ensure_states() {
        if (!available()) return false;
        for (void*& state : states_) if (state == nullptr) {
            state = create_(nullptr);
            if (state == nullptr) { reset_states(false); return false; }
        }
        return true;
    }
    void* library_{nullptr};
    CreateFn create_{nullptr}; DestroyFn destroy_{nullptr}; ProcessFn process_{nullptr};
    std::array<void*, CHANNELS> states_{};
    std::array<std::array<std::array<float, CHUNK_FRAMES>, CHANNELS>, 2> dry_delay_{};
    int dry_delay_slot_{0};
};

struct CompressorState {
    float envelope{0.0f};
    float gain{1.0f};
    float reduction_db{0.0f};
    float meter_reduction_db{0.0f};
    int meter_hold_chunks{0};
};

struct DeEsserState {
    // Split filter used both to preserve the low/mid voice body and to attenuate
    // only the high band when true sibilance is detected.
    std::array<float, CHANNELS> split_lowpass{};
    // A second low-pass limits the detector to the useful sibilance region;
    // energy above this point is ignored for detection so broadband hiss does
    // not dominate the decision.
    std::array<float, CHANNELS> detector_top_lowpass{};
    float sibilance_energy{0.0f};
    float speech_energy{0.0f};
    float high_gain{1.0f};
    float reduction_db{0.0f};
    float meter_reduction_db{0.0f};
    int meter_hold_chunks{0};
};

struct LimiterState {
    float gain{1.0f};
    float reduction_db{0.0f};
    float meter_reduction_db{0.0f};
    int meter_hold_chunks{0};
};

struct SoftGateState {
    bool open{true};
    int hold_chunks{0};
    float gain{1.0f};
};

float db_to_gain(float db) {
    return std::pow(10.0f, db / 20.0f);
}

float gain_to_db(float gain) {
    return 20.0f * std::log10(std::max(0.000001f, gain));
}

void update_reduction_meter(float instant_db, float& meter_db, int& hold_chunks) {
    const float instant = clampf(instant_db, 0.0f, 60.0f);
    constexpr int hold_for_chunks = 32;  // ~320 ms at the normal 10 ms period.
    constexpr float decay_per_chunk = 0.90f;

    if (instant >= meter_db) {
        meter_db = instant;
        hold_chunks = hold_for_chunks;
        return;
    }
    if (hold_chunks > 0) {
        --hold_chunks;
        return;
    }
    meter_db = std::max(instant, meter_db * decay_per_chunk);
    if (meter_db < 0.05f) meter_db = 0.0f;
}

void apply_gain_db(std::vector<float>& frames, float gain_db) {
    const float gain = db_to_gain(clampf(gain_db, -12.0f, 24.0f));
    if (std::fabs(gain - 1.0f) < 0.0001f) {
        return;
    }
    for (float& sample : frames) {
        sample = std::isfinite(sample) ? sample * gain : 0.0f;
    }
}

float soft_gate_threshold_peak(float threshold_percent) {
    const float normalized = clampf(threshold_percent, 0.0f, 100.0f) / 100.0f;
    // Gentle voice gate range: roughly -60 dBFS .. -18 dBFS. The Hard Gate
    // remains the separate absolute cut-off tool with its own stronger curve.
    return db_to_gain(-60.0f + normalized * 42.0f);
}

void apply_soft_gate(std::vector<float>& frames, SoftGateState& dsp, const Settings& state) {
    if (!state.processing_enabled || !state.gate_enabled || frames.empty()) {
        dsp.open = true;
        dsp.hold_chunks = 0;
        dsp.gain = 1.0f;
        return;
    }

    float peak = 0.0f;
    for (float sample : frames) {
        if (std::isfinite(sample)) peak = std::max(peak, std::fabs(sample));
    }

    const float open_threshold = soft_gate_threshold_peak(state.gate_threshold);
    const float close_threshold = std::max(0.0001f, open_threshold * 0.58f);
    if (peak >= open_threshold) {
        dsp.open = true;
        dsp.hold_chunks = 12; // ~120 ms at the native 10 ms MICRO cadence.
    } else if (dsp.open && peak < close_threshold) {
        if (dsp.hold_chunks > 0) {
            --dsp.hold_chunks;
        } else {
            dsp.open = false;
        }
    }

    // Normal Gate is deliberately not a hard mute. Closed state is -26 dB,
    // with a fast opening and long release so syllable tails remain natural.
    const float target_gain = dsp.open ? 1.0f : db_to_gain(-26.0f);
    const float tau_seconds = target_gain > dsp.gain ? 0.004f : 0.120f;
    const float coefficient = std::exp(-1.0f / (tau_seconds * static_cast<float>(RATE)));
    constexpr int channels = 2;
    const int frame_count = std::max(1, static_cast<int>(frames.size()) / channels);
    for (int frame = 0; frame < frame_count; ++frame) {
        dsp.gain = target_gain + (dsp.gain - target_gain) * coefficient;
        for (int ch = 0; ch < channels; ++ch) {
            const std::size_t index = static_cast<std::size_t>(frame * channels + ch);
            const float sample = std::isfinite(frames[index]) ? frames[index] : 0.0f;
            frames[index] = sample * dsp.gain;
        }
    }
}

void apply_compressor(std::vector<float>& frames, CompressorState& dsp, const Settings& state) {
    dsp.reduction_db = 0.0f;
    if (!state.compressor_enabled || state.compressor_amount <= 0.0f || frames.empty()) {
        dsp.envelope = 0.0f;
        dsp.gain = 1.0f;
        dsp.meter_reduction_db = 0.0f;
        dsp.meter_hold_chunks = 0;
        return;
    }

    const float amount = clampf(state.compressor_amount, 0.0f, 100.0f) / 100.0f;
    const float threshold_db = -10.0f - (26.0f * amount);
    const float ratio = 1.5f + (6.5f * amount);
    const float makeup_db = 0.5f + (6.0f * amount);
    const float detector_attack = std::exp(-1.0f / (0.006f * RATE));
    const float detector_release = std::exp(-1.0f / (0.120f * RATE));
    const float gain_attack = std::exp(-1.0f / (0.003f * RATE));
    const float gain_release = std::exp(-1.0f / (0.090f * RATE));

    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;
    for (int frame = 0; frame < frame_count; ++frame) {
        float peak = 0.0f;
        for (int ch = 0; ch < CHANNELS; ++ch) {
            const float sample = frames[frame * CHANNELS + ch];
            if (std::isfinite(sample)) {
                peak = std::max(peak, std::fabs(sample));
            }
        }

        const float detector_coeff = peak > dsp.envelope ? detector_attack : detector_release;
        dsp.envelope = (detector_coeff * dsp.envelope) + ((1.0f - detector_coeff) * peak);
        const float level_db = gain_to_db(dsp.envelope);
        const float over_db = std::max(0.0f, level_db - threshold_db);
        const float reduction_db = -over_db * (1.0f - (1.0f / ratio));
        dsp.reduction_db = std::max(dsp.reduction_db, -reduction_db);
        const float target_gain = db_to_gain(reduction_db + makeup_db);
        const float gain_coeff = target_gain < dsp.gain ? gain_attack : gain_release;
        dsp.gain = (gain_coeff * dsp.gain) + ((1.0f - gain_coeff) * target_gain);

        for (int ch = 0; ch < CHANNELS; ++ch) {
            float& sample = frames[frame * CHANNELS + ch];
            sample = std::isfinite(sample) ? sample * dsp.gain : 0.0f;
        }
    }
    update_reduction_meter(dsp.reduction_db, dsp.meter_reduction_db, dsp.meter_hold_chunks);
}

void apply_deesser(std::vector<float>& frames, DeEsserState& dsp, const Settings& state) {
    dsp.reduction_db = 0.0f;
    if (!state.deesser_enabled || state.deesser_amount <= 0.0f || frames.empty()) {
        dsp = DeEsserState{};
        return;
    }

    const float amount = clampf(state.deesser_amount, 0.0f, 100.0f) / 100.0f;

    // Detect a band instead of simply looking at "everything above X Hz".
    // S/SH/CH consonants concentrate much more of their energy in roughly
    // 3.4-10.5 kHz than normal voiced vowels. Comparing that band against the
    // low/mid speech body makes the detector far less sensitive to ordinary
    // bright vowels and microphone level.
    constexpr float split_hz = 3400.0f;
    constexpr float detector_top_hz = 10500.0f;
    const float split_coeff = std::exp(-2.0f * 3.14159265358979323846f * split_hz / RATE);
    const float detector_top_coeff = std::exp(-2.0f * 3.14159265358979323846f * detector_top_hz / RATE);

    const float energy_attack = std::exp(-1.0f / (0.0020f * RATE));
    const float energy_release = std::exp(-1.0f / (0.035f * RATE));
    const float gain_attack = std::exp(-1.0f / (0.0015f * RATE));
    const float gain_release = std::exp(-1.0f / (0.055f * RATE));

    // Amount increases both sensitivity and maximum reduction, but even at
    // 100% the detector still needs the sibilance band to dominate the speech
    // body. This prevents the old "AAAA is the same as SSSS" behaviour.
    const float ratio_threshold = 0.42f - (0.15f * amount);
    constexpr float ratio_full_scale = 0.62f;
    const float max_reduction_db = 3.0f + (9.0f * amount);

    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;
    for (int frame = 0; frame < frame_count; ++frame) {
        std::array<float, CHANNELS> low{};
        std::array<float, CHANNELS> high{};
        float frame_sibilance_energy = 0.0f;
        float frame_speech_energy = 0.0f;

        for (int ch = 0; ch < CHANNELS; ++ch) {
            const int index = frame * CHANNELS + ch;
            const float sample = std::isfinite(frames[index]) ? frames[index] : 0.0f;

            dsp.split_lowpass[ch] =
                (split_coeff * dsp.split_lowpass[ch]) + ((1.0f - split_coeff) * sample);
            dsp.detector_top_lowpass[ch] =
                (detector_top_coeff * dsp.detector_top_lowpass[ch])
                + ((1.0f - detector_top_coeff) * sample);

            low[ch] = dsp.split_lowpass[ch];
            high[ch] = sample - low[ch];

            const float detector_band = dsp.detector_top_lowpass[ch] - low[ch];
            frame_sibilance_energy = std::max(
                frame_sibilance_energy,
                detector_band * detector_band
            );
            frame_speech_energy = std::max(
                frame_speech_energy,
                low[ch] * low[ch]
            );
        }

        const float sib_coeff = frame_sibilance_energy > dsp.sibilance_energy
            ? energy_attack : energy_release;
        const float speech_coeff = frame_speech_energy > dsp.speech_energy
            ? energy_attack : energy_release;
        dsp.sibilance_energy =
            (sib_coeff * dsp.sibilance_energy)
            + ((1.0f - sib_coeff) * frame_sibilance_energy);
        dsp.speech_energy =
            (speech_coeff * dsp.speech_energy)
            + ((1.0f - speech_coeff) * frame_speech_energy);

        const float sibilance_rms = std::sqrt(std::max(0.0f, dsp.sibilance_energy));
        const float speech_rms = std::sqrt(std::max(0.0f, dsp.speech_energy));
        const float spectral_ratio = sibilance_rms
            / std::max(0.000001f, sibilance_rms + speech_rms);

        const float ratio_span = std::max(0.08f, ratio_full_scale - ratio_threshold);
        const float sibilance = clampf(
            (spectral_ratio - ratio_threshold) / ratio_span,
            0.0f,
            1.0f
        );

        // Require meaningful energy inside the sibilance band itself. This
        // keeps quiet room hiss and numerical noise from opening the detector.
        const float sibilance_level_db = gain_to_db(std::max(sibilance_rms, 0.000001f));
        const float level_weight = clampf(
            (sibilance_level_db + 52.0f) / 22.0f,
            0.0f,
            1.0f
        );

        const float target_gain = db_to_gain(-max_reduction_db * sibilance * level_weight);
        const float gain_coeff = target_gain < dsp.high_gain ? gain_attack : gain_release;
        dsp.high_gain =
            (gain_coeff * dsp.high_gain) + ((1.0f - gain_coeff) * target_gain);
        dsp.reduction_db = std::max(
            dsp.reduction_db,
            std::max(0.0f, -gain_to_db(dsp.high_gain))
        );

        for (int ch = 0; ch < CHANNELS; ++ch) {
            frames[frame * CHANNELS + ch] = low[ch] + (high[ch] * dsp.high_gain);
        }
    }
    update_reduction_meter(dsp.reduction_db, dsp.meter_reduction_db, dsp.meter_hold_chunks);
}

void apply_limiter(std::vector<float>& frames, LimiterState& dsp, bool enabled, float ceiling_db) {
    dsp.reduction_db = 0.0f;
    if (!enabled || frames.empty()) {
        dsp.gain = 1.0f;
        dsp.meter_reduction_db = 0.0f;
        dsp.meter_hold_chunks = 0;
        return;
    }

    const float ceiling = db_to_gain(clampf(ceiling_db, -12.0f, 0.0f));
    const float release_coeff = std::exp(-1.0f / (0.080f * RATE));
    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;
    for (int frame = 0; frame < frame_count; ++frame) {
        float peak = 0.0f;
        for (int ch = 0; ch < CHANNELS; ++ch) {
            const float sample = frames[frame * CHANNELS + ch];
            if (std::isfinite(sample)) {
                peak = std::max(peak, std::fabs(sample));
            }
        }

        const float target = peak > ceiling ? ceiling / std::max(peak, 0.000001f) : 1.0f;
        if (target < dsp.gain) {
            dsp.gain = target; // zero-lookahead safety attack
        } else {
            dsp.gain = (release_coeff * dsp.gain) + ((1.0f - release_coeff) * target);
        }
        dsp.reduction_db = std::max(dsp.reduction_db, std::max(0.0f, -gain_to_db(dsp.gain)));

        for (int ch = 0; ch < CHANNELS; ++ch) {
            float& sample = frames[frame * CHANNELS + ch];
            sample = std::isfinite(sample) ? sample * dsp.gain : 0.0f;
        }
    }
    update_reduction_meter(dsp.reduction_db, dsp.meter_reduction_db, dsp.meter_hold_chunks);
}


} // namespace

struct Processor::Impl {
    RNNoiseBackend rnnoise;
    ParametricEqState eq;
    SoftGateState gate;
    CompressorState compressor;
    DeEsserState deesser;
    LimiterState limiter;
};

Processor::Processor() : impl_(std::make_unique<Impl>()) {}
Processor::~Processor() = default;
Processor::Processor(Processor&&) noexcept = default;
Processor& Processor::operator=(Processor&&) noexcept = default;

void Processor::reset() {
    impl_->rnnoise.reset_states(false);
    impl_->eq = ParametricEqState{};
    impl_->gate = SoftGateState{};
    impl_->compressor = CompressorState{};
    impl_->deesser = DeEsserState{};
    impl_->limiter = LimiterState{};
}

bool Processor::rnnoise_available() const {
    return impl_->rnnoise.available();
}

void Processor::process_pre_gate(std::vector<float>& frames, const Settings& settings) {
    if (!settings.processing_enabled) {
        return;
    }
    apply_gain_db(frames, settings.input_gain_db);
    if (settings.noise_suppression_enabled) {
        impl_->rnnoise.process(frames, settings.noise_suppression);
    } else {
        impl_->rnnoise.reset_states(false);
    }
}

void Processor::process_post_gate(std::vector<float>& frames, const Settings& settings) {
    if (settings.processing_enabled) {
        apply_soft_gate(frames, impl_->gate, settings);
        apply_parametric_eq(frames, impl_->eq, settings);
        apply_compressor(frames, impl_->compressor, settings);
        apply_deesser(frames, impl_->deesser, settings);
        return;
    }
    impl_->eq = ParametricEqState{};
    impl_->gate = SoftGateState{};
    impl_->compressor = CompressorState{};
    impl_->deesser = DeEsserState{};
}

void Processor::process_final_mix(std::vector<float>& frames, const Settings& settings) {
    apply_limiter(
        frames,
        impl_->limiter,
        settings.processing_enabled && settings.limiter_enabled,
        settings.limiter_ceiling_db
    );
}

Telemetry Processor::telemetry() const {
    return {
        impl_->compressor.meter_reduction_db,
        impl_->deesser.meter_reduction_db,
        impl_->limiter.meter_reduction_db,
    };
}

} // namespace ksound::micro_tune
