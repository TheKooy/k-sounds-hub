#pragma once

#include <memory>
#include <span>
#include <vector>

namespace ksound::micro_tune {

struct EqBandSpec {
    float frequency{1000.0f};
    float gain_db{0.0f};
    float q{1.0f};
};

struct Settings {
    bool processing_enabled{false};
    float input_gain_db{0.0f};
    bool noise_suppression_enabled{false};
    float noise_suppression{0.0f};
    bool gate_enabled{false};
    float gate_threshold{35.0f};
    std::span<const EqBandSpec> eq_bands{};
    bool compressor_enabled{false};
    float compressor_amount{45.0f};
    bool deesser_enabled{false};
    float deesser_amount{35.0f};
    bool limiter_enabled{true};
    float limiter_ceiling_db{-1.0f};
};

struct Telemetry {
    float compressor_reduction_db{0.0f};
    float deesser_reduction_db{0.0f};
    float limiter_reduction_db{0.0f};
};

class Processor {
public:
    Processor();
    ~Processor();

    Processor(const Processor&) = delete;
    Processor& operator=(const Processor&) = delete;
    Processor(Processor&&) noexcept;
    Processor& operator=(Processor&&) noexcept;

    void reset();
    bool rnnoise_available() const;
    void process_pre_gate(std::vector<float>& frames, const Settings& settings);
    void process_post_gate(std::vector<float>& frames, const Settings& settings);
    void process_final_mix(std::vector<float>& frames, const Settings& settings);
    Telemetry telemetry() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace ksound::micro_tune
