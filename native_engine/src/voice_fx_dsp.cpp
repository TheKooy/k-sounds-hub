#include "voice_fx_dsp.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <memory>
#include <utility>
#include <vector>

namespace ksound::voice_fx {
namespace {
constexpr int RATE = 48000;
constexpr int CHANNELS = 2;

float clampf(float value, float low, float high) {
    return std::max(low, std::min(high, value));
}

float db_to_gain(float db) {
    return std::pow(10.0f, db / 20.0f);
}

struct MicroState {
    std::vector<std::string> voice_fx_active;
    float voice_fx_amount{72.0f};
    bool custom_fx_enabled{false};
    float custom_fx_wet{100.0f};
    std::vector<CustomModuleSpec> custom_fx_modules;
};

struct VoiceFxDsp {
    std::array<float, CHANNELS> hp_prev_in{};
    std::array<float, CHANNELS> hp_prev_out{};
    std::array<float, CHANNELS> hp2_prev_in{};
    std::array<float, CHANNELS> hp2_prev_out{};
    std::array<float, CHANNELS> lp_state{};
    std::array<float, CHANNELS> lp2_state{};
    std::array<float, CHANNELS> lp_mid_state{};
    std::array<float, CHANNELS> sample_hold{};
    int decimation_phase{0};

    std::array<std::vector<float>, CHANNELS> predelay{};
    std::array<std::vector<float>, CHANNELS> comb_a{};
    std::array<std::vector<float>, CHANNELS> comb_b{};
    std::array<std::vector<float>, CHANNELS> comb_c{};
    std::array<std::vector<float>, CHANNELS> comb_d{};
    std::array<std::vector<float>, CHANNELS> allpass_a{};
    std::array<std::vector<float>, CHANNELS> allpass_b{};
    std::array<std::size_t, CHANNELS> predelay_index{};
    std::array<std::size_t, CHANNELS> comb_a_index{};
    std::array<std::size_t, CHANNELS> comb_b_index{};
    std::array<std::size_t, CHANNELS> comb_c_index{};
    std::array<std::size_t, CHANNELS> comb_d_index{};
    std::array<std::size_t, CHANNELS> allpass_a_index{};
    std::array<std::size_t, CHANNELS> allpass_b_index{};
    std::array<float, CHANNELS> reverb_damp_a{};
    std::array<float, CHANNELS> reverb_damp_b{};
    std::array<float, CHANNELS> reverb_damp_c{};
    std::array<float, CHANNELS> reverb_damp_d{};

    std::array<std::vector<float>, CHANNELS> pitch_buffer{};
    std::array<std::size_t, CHANNELS> pitch_write{};
    float pitch_phase{0.0f};
    float robot_phase{0.0f};
    float texture_phase{0.0f};
    float wet_current{0.0f};
    bool active_last{false};

    void reset(const std::string& effect) {
        hp_prev_in.fill(0.0f);
        hp_prev_out.fill(0.0f);
        hp2_prev_in.fill(0.0f);
        hp2_prev_out.fill(0.0f);
        lp_state.fill(0.0f);
        lp2_state.fill(0.0f);
        lp_mid_state.fill(0.0f);
        sample_hold.fill(0.0f);
        decimation_phase = 0;
        pitch_write.fill(0);
        pitch_phase = 0.0f;
        robot_phase = 0.0f;
        texture_phase = 0.0f;
        wet_current = 0.0f;

        if (effect == "church" || effect == "church_pa" || effect == "reverb") {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                if (effect == "reverb") {
                    predelay[ch].assign(static_cast<std::size_t>(827 + ch * 61), 0.0f); // ~17-18 ms
                    comb_a[ch].assign(static_cast<std::size_t>(1153 + ch * 29), 0.0f);
                    comb_b[ch].assign(static_cast<std::size_t>(1531 + ch * 37), 0.0f);
                    comb_c[ch].assign(static_cast<std::size_t>(1907 + ch * 43), 0.0f);
                    comb_d[ch].assign(static_cast<std::size_t>(2371 + ch * 47), 0.0f);
                    allpass_a[ch].assign(static_cast<std::size_t>(263 + ch * 12), 0.0f);
                    allpass_b[ch].assign(static_cast<std::size_t>(421 + ch * 16), 0.0f);
                } else if (effect == "church_pa") {
                    // Slightly longer propagation and darker diffusion for a PA
                    // feed arriving from loudspeakers into the same large space.
                    predelay[ch].assign(static_cast<std::size_t>(3121 + ch * 173), 0.0f); // ~65-68 ms
                    comb_a[ch].assign(static_cast<std::size_t>(1789 + ch * 31), 0.0f);  // ~37 ms
                    comb_b[ch].assign(static_cast<std::size_t>(2297 + ch * 41), 0.0f);  // ~48 ms
                    comb_c[ch].assign(static_cast<std::size_t>(3011 + ch * 47), 0.0f);  // ~63 ms
                    comb_d[ch].assign(static_cast<std::size_t>(3677 + ch * 59), 0.0f);  // ~77 ms
                    allpass_a[ch].assign(static_cast<std::size_t>(401 + ch * 16), 0.0f);
                    allpass_b[ch].assign(static_cast<std::size_t>(613 + ch * 20), 0.0f);
                } else {
                    // Wide, decorrelated room times: avoid the tight 32-45 ms
                    // cluster that produced a metallic/robotic repeat in IP13b3.
                    predelay[ch].assign(static_cast<std::size_t>(2591 + ch * 149), 0.0f); // ~54-57 ms
                    comb_a[ch].assign(static_cast<std::size_t>(1499 + ch * 29), 0.0f);  // ~31 ms
                    comb_b[ch].assign(static_cast<std::size_t>(2111 + ch * 37), 0.0f);  // ~44 ms
                    comb_c[ch].assign(static_cast<std::size_t>(2777 + ch * 43), 0.0f);  // ~58 ms
                    comb_d[ch].assign(static_cast<std::size_t>(3371 + ch * 53), 0.0f);  // ~70 ms
                    allpass_a[ch].assign(static_cast<std::size_t>(347 + ch * 14), 0.0f);
                    allpass_b[ch].assign(static_cast<std::size_t>(571 + ch * 18), 0.0f);
                }
            }
            predelay_index.fill(0);
            comb_a_index.fill(0);
            comb_b_index.fill(0);
            comb_c_index.fill(0);
            comb_d_index.fill(0);
            allpass_a_index.fill(0);
            allpass_b_index.fill(0);
            reverb_damp_a.fill(0.0f);
            reverb_damp_b.fill(0.0f);
            reverb_damp_c.fill(0.0f);
            reverb_damp_d.fill(0.0f);
        }

        if (effect == "pitch_down" || effect == "pitch_up") {
            constexpr std::size_t pitch_buffer_samples = 4096;
            for (auto& buffer : pitch_buffer) buffer.assign(pitch_buffer_samples, 0.0f);
            pitch_write.fill(0);
            pitch_phase = 0.0f;
        }
    }
};

struct VoiceFxChainDsp {
    VoiceFxDsp distortion;
    VoiceFxDsp pitch_down;
    VoiceFxDsp pitch_up;
    VoiceFxDsp radio;
    VoiceFxDsp telephone;
    VoiceFxDsp robot;
    VoiceFxDsp reverb;
    VoiceFxDsp church;
    VoiceFxDsp church_pa;
};

float voice_fx_lowpass(float input, float cutoff_hz, float& state) {
    constexpr float pi = 3.14159265358979323846f;
    const float cutoff = clampf(cutoff_hz, 40.0f, 18000.0f);
    const float dt = 1.0f / static_cast<float>(RATE);
    const float rc = 1.0f / (2.0f * pi * cutoff);
    const float alpha = dt / (rc + dt);
    state += alpha * (input - state);
    return state;
}

float voice_fx_highpass(
    float input,
    float cutoff_hz,
    float& previous_input,
    float& previous_output
) {
    constexpr float pi = 3.14159265358979323846f;
    const float cutoff = clampf(cutoff_hz, 40.0f, 18000.0f);
    const float dt = 1.0f / static_cast<float>(RATE);
    const float rc = 1.0f / (2.0f * pi * cutoff);
    const float alpha = rc / (rc + dt);
    const float output = alpha * (previous_output + input - previous_input);
    previous_input = input;
    previous_output = std::isfinite(output) ? output : 0.0f;
    return previous_output;
}

float voice_fx_comb(
    float input,
    std::vector<float>& buffer,
    std::size_t& index,
    float feedback,
    float damping,
    float& damp_state
) {
    if (buffer.empty()) return input;
    float delayed = buffer[index];
    damp_state = delayed * (1.0f - damping) + damp_state * damping;
    buffer[index] = input + damp_state * feedback;
    index = (index + 1) % buffer.size();
    return delayed;
}

float voice_fx_allpass(float input, std::vector<float>& buffer, std::size_t& index, float feedback) {
    if (buffer.empty()) return input;
    const float delayed = buffer[index];
    const float output = delayed - input;
    buffer[index] = input + delayed * feedback;
    index = (index + 1) % buffer.size();
    return output;
}

float voice_fx_pitch_read(const std::vector<float>& buffer, std::size_t write, float delay) {
    if (buffer.empty()) return 0.0f;
    float position = static_cast<float>(write) - delay;
    const float size = static_cast<float>(buffer.size());
    while (position < 0.0f) position += size;
    while (position >= size) position -= size;
    const std::size_t i0 = static_cast<std::size_t>(position) % buffer.size();
    const std::size_t i1 = (i0 + 1) % buffer.size();
    const float fraction = position - std::floor(position);
    return buffer[i0] + (buffer[i1] - buffer[i0]) * fraction;
}

void apply_single_voice_fx(
    std::vector<float>& frames,
    VoiceFxDsp& dsp,
    const std::string& effect,
    bool enabled,
    float amount_percent
) {
    if (frames.empty()) return;

    if (enabled && !dsp.active_last) {
        dsp.reset(effect);
    }

    const float target_wet = enabled ? clampf(amount_percent, 0.0f, 100.0f) / 100.0f : 0.0f;
    if (target_wet <= 0.0001f && dsp.wet_current <= 0.0001f) {
        dsp.wet_current = 0.0f;
        dsp.active_last = enabled;
        return;
    }

    const std::vector<float> dry = frames;
    std::vector<float> wet = frames;
    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;

    if (effect == "distortion") {
        constexpr float drive = 7.0f;
        const float norm = std::max(0.0001f, std::tanh(drive));
        for (float& sample : wet) {
            const float safe = std::isfinite(sample) ? sample : 0.0f;
            sample = clampf(std::tanh(safe * drive) / norm, -1.0f, 1.0f);
        }
    } else if (effect == "pitch_down" || effect == "pitch_up") {
        constexpr float pi = 3.14159265358979323846f;
        constexpr float window = 1800.0f;
        constexpr float min_delay = 64.0f;
        const float factor = effect == "pitch_up" ? 1.38f : 0.72f;
        const float phase_step = std::fabs(1.0f - factor) / window;
        for (int frame = 0; frame < frame_count; ++frame) {
            const float phase_a = dsp.pitch_phase;
            float phase_b = phase_a + 0.5f;
            if (phase_b >= 1.0f) phase_b -= 1.0f;
            const auto delay_for = [&](float phase) {
                return min_delay + (factor > 1.0f ? (1.0f - phase) : phase) * window;
            };
            const float weight_a = 0.5f - 0.5f * std::cos(2.0f * pi * phase_a);
            const float weight_b = 1.0f - weight_a;
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                auto& buffer = dsp.pitch_buffer[ch];
                auto& write = dsp.pitch_write[ch];
                if (buffer.empty()) buffer.assign(4096, 0.0f);
                const float input = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                buffer[write] = input;
                const float a = voice_fx_pitch_read(buffer, write, delay_for(phase_a));
                const float b = voice_fx_pitch_read(buffer, write, delay_for(phase_b));
                wet[index] = clampf((a * weight_a + b * weight_b) * 1.08f, -1.0f, 1.0f);
                write = (write + 1) % buffer.size();
            }
            dsp.pitch_phase += phase_step;
            if (dsp.pitch_phase >= 1.0f) dsp.pitch_phase -= 1.0f;
        }
    } else if (effect == "radio") {
        constexpr float pi = 3.14159265358979323846f;
        const float texture_step = 2.0f * pi * 3650.0f / static_cast<float>(RATE);
        for (int frame = 0; frame < frame_count; ++frame) {
            const float texture = std::sin(dsp.texture_phase);
            dsp.texture_phase += texture_step;
            if (dsp.texture_phase >= 2.0f * pi) dsp.texture_phase -= 2.0f * pi;
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                float sample = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                sample = voice_fx_highpass(sample, 360.0f, dsp.hp_prev_in[ch], dsp.hp_prev_out[ch]);
                sample = voice_fx_lowpass(sample, 3150.0f, dsp.lp_state[ch]);
                // Slightly stronger pilot/intercom character than IP13b3: more
                // compression, a tighter band and audible comms grit at 100%.
                sample = std::tanh(sample * 5.0f) / std::tanh(5.0f);
                sample = std::round(sample * 56.0f) / 56.0f;
                if (std::fabs(sample) > 0.010f) sample += texture * 0.014f;
                wet[index] = clampf(sample * 0.94f, -1.0f, 1.0f);
            }
        }
    } else if (effect == "telephone") {
        // Old-handset / narrowband line simulation. The 8 kHz sample-and-hold
        // makes this immediately distinct from Radio instead of merely sounding
        // like a slightly worse microphone.
        for (int frame = 0; frame < frame_count; ++frame) {
            const bool capture = dsp.decimation_phase == 0;
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                float sample = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                sample = voice_fx_lowpass(sample, 3350.0f, dsp.lp_state[ch]);
                if (capture) dsp.sample_hold[ch] = sample;
                sample = dsp.sample_hold[ch];
                sample = voice_fx_highpass(sample, 430.0f, dsp.hp_prev_in[ch], dsp.hp_prev_out[ch]);
                sample = voice_fx_highpass(sample, 430.0f, dsp.hp2_prev_in[ch], dsp.hp2_prev_out[ch]);
                sample = voice_fx_lowpass(sample, 2850.0f, dsp.lp2_state[ch]);
                const float low_mid = voice_fx_lowpass(sample, 1050.0f, dsp.lp_mid_state[ch]);
                const float nasal = sample - low_mid;
                sample = (sample * 0.56f) + (nasal * 0.94f);
                sample = std::tanh(sample * 5.6f) / std::tanh(5.6f);
                sample = std::round(sample * 44.0f) / 44.0f;
                wet[index] = clampf(sample * 0.96f, -1.0f, 1.0f);
            }
            dsp.decimation_phase = (dsp.decimation_phase + 1) % 6;
        }
    } else if (effect == "robot") {
        // Keep the validated IP13a Robot DSP unchanged: this is Robot 1.
        constexpr float pi = 3.14159265358979323846f;
        constexpr float carrier_hz = 68.0f;
        const float phase_step = 2.0f * pi * carrier_hz / static_cast<float>(RATE);
        for (int frame = 0; frame < frame_count; ++frame) {
            const float carrier = std::sin(dsp.robot_phase);
            dsp.robot_phase += phase_step;
            if (dsp.robot_phase >= 2.0f * pi) dsp.robot_phase -= 2.0f * pi;
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                const float sample = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                wet[index] = std::tanh(sample * 1.8f) * carrier * 0.90f;
            }
        }
    } else if (effect == "reverb") {
        for (int frame = 0; frame < frame_count; ++frame) {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                const float direct = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                float input = voice_fx_highpass(direct, 110.0f, dsp.hp_prev_in[ch], dsp.hp_prev_out[ch]);
                float predelayed = input;
                if (!dsp.predelay[ch].empty()) {
                    auto& buffer = dsp.predelay[ch];
                    auto& cursor = dsp.predelay_index[ch];
                    predelayed = buffer[cursor];
                    buffer[cursor] = input;
                    cursor = (cursor + 1) % buffer.size();
                }
                const float a = voice_fx_comb(predelayed, dsp.comb_a[ch], dsp.comb_a_index[ch], 0.72f, 0.36f, dsp.reverb_damp_a[ch]);
                const float b = voice_fx_comb(predelayed, dsp.comb_b[ch], dsp.comb_b_index[ch], 0.68f, 0.39f, dsp.reverb_damp_b[ch]);
                const float c = voice_fx_comb(predelayed, dsp.comb_c[ch], dsp.comb_c_index[ch], 0.64f, 0.42f, dsp.reverb_damp_c[ch]);
                const float d = voice_fx_comb(predelayed, dsp.comb_d[ch], dsp.comb_d_index[ch], 0.60f, 0.45f, dsp.reverb_damp_d[ch]);
                float tail = (a + b + c + d) * 0.25f;
                tail = voice_fx_allpass(tail, dsp.allpass_a[ch], dsp.allpass_a_index[ch], 0.55f);
                tail = voice_fx_allpass(tail, dsp.allpass_b[ch], dsp.allpass_b_index[ch], 0.48f);
                wet[index] = clampf((direct * 0.28f) + (predelayed * 0.14f) + (tail * 0.58f), -1.0f, 1.0f);
            }
        }
    } else if (effect == "church" || effect == "church_pa") {
        if (dsp.predelay[0].empty()) dsp.reset(effect);
        const bool pa_mode = effect == "church_pa";
        for (int frame = 0; frame < frame_count; ++frame) {
            for (int ch = 0; ch < CHANNELS; ++ch) {
                const int index = frame * CHANNELS + ch;
                const float input = std::isfinite(wet[index]) ? wet[index] : 0.0f;

                float direct = input;
                float reverb_send = input;
                if (pa_mode) {
                    // Church PA: keep the amplified microphone voice at a normal
                    // foreground level, while a loudspeaker-coloured copy excites
                    // the room behind it. This gives an intelligible PA voice first
                    // and preserves the existing church echo level afterwards.
                    float speaker = voice_fx_highpass(input, 165.0f, dsp.hp_prev_in[ch], dsp.hp_prev_out[ch]);
                    speaker = voice_fx_lowpass(speaker, 6500.0f, dsp.lp_state[ch]);
                    const float low_mid = voice_fx_lowpass(speaker, 1200.0f, dsp.lp2_state[ch]);
                    const float presence = speaker - low_mid;
                    speaker = (speaker * 0.80f) + (presence * 0.24f);
                    speaker = std::tanh(speaker * 2.7f) / std::tanh(2.7f);
                    direct = clampf(speaker, -1.0f, 1.0f);
                    reverb_send = direct;
                } else {
                    // De-plosive the reverb send only. The direct voice remains
                    // natural, while low-frequency P/B bursts no longer excite
                    // the long church tail into a large audible puff.
                    reverb_send = voice_fx_highpass(input, 155.0f, dsp.hp_prev_in[ch], dsp.hp_prev_out[ch]);
                    reverb_send = std::tanh(reverb_send * 1.35f) / std::tanh(1.35f);
                }

                auto& pre_buf = dsp.predelay[ch];
                auto& pre_idx = dsp.predelay_index[ch];
                const float predelayed = pre_buf[pre_idx];
                pre_buf[pre_idx] = reverb_send;
                pre_idx = (pre_idx + 1) % pre_buf.size();

                const float a = voice_fx_comb(predelayed, dsp.comb_a[ch], dsp.comb_a_index[ch], pa_mode ? 0.80f : 0.78f, 0.50f, dsp.reverb_damp_a[ch]);
                const float b = voice_fx_comb(predelayed, dsp.comb_b[ch], dsp.comb_b_index[ch], pa_mode ? 0.83f : 0.82f, 0.54f, dsp.reverb_damp_b[ch]);
                const float c = voice_fx_comb(predelayed, dsp.comb_c[ch], dsp.comb_c_index[ch], pa_mode ? 0.85f : 0.85f, 0.58f, dsp.reverb_damp_c[ch]);
                const float d = voice_fx_comb(predelayed, dsp.comb_d[ch], dsp.comb_d_index[ch], pa_mode ? 0.86f : 0.86f, 0.62f, dsp.reverb_damp_d[ch]);
                float tail = (a + b + c + d) * 0.25f;
                tail = voice_fx_allpass(tail, dsp.allpass_a[ch], dsp.allpass_a_index[ch], pa_mode ? 0.56f : 0.58f);
                tail = voice_fx_allpass(tail, dsp.allpass_b[ch], dsp.allpass_b_index[ch], pa_mode ? 0.52f : 0.54f);
                tail = voice_fx_lowpass(tail, pa_mode ? 4400.0f : 5100.0f, dsp.lp_mid_state[ch]);

                if (pa_mode) {
                    // Keep the PA/micro voice at normal level, then add the same
                    // church reflection level behind it. A gentle soft ceiling only
                    // catches summed peaks; it does not deliberately turn the direct
                    // voice down to make room for the reverb.
                    const float pa_mix = direct + (predelayed * 0.08f) + (tail * 0.54f);
                    wet[index] = clampf(std::tanh(pa_mix * 1.08f) / std::tanh(1.08f), -1.0f, 1.0f);
                } else {
                    // Preserve some direct voice at full wet while keeping the
                    // large diffuse room. This also keeps plosives from being
                    // represented almost entirely by reverberant low-frequency energy.
                    wet[index] = clampf((direct * 0.18f) + (predelayed * 0.08f) + (tail * 0.74f), -1.0f, 1.0f);
                }
            }
        }
    }

    const float start_wet = clampf(dsp.wet_current, 0.0f, 1.0f);
    const float end_wet = clampf(target_wet, 0.0f, 1.0f);
    for (int frame = 0; frame < frame_count; ++frame) {
        const float progress = static_cast<float>(frame + 1) / static_cast<float>(std::max(1, frame_count));
        const float mix = start_wet + ((end_wet - start_wet) * progress);
        for (int ch = 0; ch < CHANNELS; ++ch) {
            const int index = frame * CHANNELS + ch;
            const float dry_sample = std::isfinite(dry[index]) ? dry[index] : 0.0f;
            const float wet_sample = std::isfinite(wet[index]) ? wet[index] : 0.0f;
            frames[index] = dry_sample + ((wet_sample - dry_sample) * mix);
        }
    }
    dsp.wet_current = end_wet;
    dsp.active_last = enabled;
}

bool voice_fx_active(const MicroState& state, const std::string& effect) {
    return std::find(state.voice_fx_active.begin(), state.voice_fx_active.end(), effect) != state.voice_fx_active.end();
}

void apply_voice_fx(std::vector<float>& frames, VoiceFxChainDsp& dsp, const MicroState& state) {
    // Fixed chain order keeps stacking deterministic. Radio/Telephone are made
    // mutually exclusive by the UI/backend, but the native layer remains safe
    // if a hand-edited state file enables both.
    const float amount = clampf(state.voice_fx_amount, 0.0f, 100.0f);
    apply_single_voice_fx(frames, dsp.distortion, "distortion", voice_fx_active(state, "distortion"), amount);
    apply_single_voice_fx(frames, dsp.pitch_down, "pitch_down", voice_fx_active(state, "pitch_down"), amount);
    apply_single_voice_fx(frames, dsp.pitch_up, "pitch_up", voice_fx_active(state, "pitch_up"), amount);
    apply_single_voice_fx(frames, dsp.radio, "radio", voice_fx_active(state, "radio"), amount);
    apply_single_voice_fx(frames, dsp.telephone, "telephone", voice_fx_active(state, "telephone"), amount);
    apply_single_voice_fx(frames, dsp.robot, "robot", voice_fx_active(state, "robot"), amount);
    apply_single_voice_fx(frames, dsp.reverb, "reverb", voice_fx_active(state, "reverb"), amount);
    apply_single_voice_fx(frames, dsp.church, "church", voice_fx_active(state, "church"), amount);
    apply_single_voice_fx(frames, dsp.church_pa, "church_pa", voice_fx_active(state, "church_pa"), amount);
}

struct CustomFxModuleDsp {
    std::string type;
    std::array<float, CHANNELS> hp_prev_in{};
    std::array<float, CHANNELS> hp_prev_out{};
    std::array<float, CHANNELS> lp_state{};
    std::array<std::vector<float>, CHANNELS> delay_buffer{};
    std::array<std::size_t, CHANNELS> delay_index{};
    // Module-owned pitch / room history, independent per ordered instance.
    VoiceFxDsp fun_dsp{};

    void reset(const std::string& wanted_type) {
        type = wanted_type;
        hp_prev_in.fill(0.0f);
        hp_prev_out.fill(0.0f);
        lp_state.fill(0.0f);
        for (auto& buffer : delay_buffer) buffer.clear();
        delay_index.fill(0);
        if (wanted_type == "pitch") fun_dsp.reset("pitch_up");
        if (wanted_type == "reverb") fun_dsp.reset("reverb");
    }
};

struct CustomFxChainDsp {
    std::vector<CustomFxModuleDsp> modules;
    float wet_current{0.0f};
    bool active_last{false};
};

void sync_custom_fx_dsp(CustomFxChainDsp& dsp, const MicroState& state) {
    if (dsp.modules.size() != state.custom_fx_modules.size()) {
        dsp.modules.resize(state.custom_fx_modules.size());
    }
    for (std::size_t i = 0; i < state.custom_fx_modules.size(); ++i) {
        const auto& spec = state.custom_fx_modules[i];
        auto& module = dsp.modules[i];
        if (module.type != spec.type) module.reset(spec.type);
        if (spec.type == "reverb") {
            // Resize pre-delay only when the user changes that parameter, never
            // on every audio chunk. The comb tail remains owned by this module.
            const std::size_t samples = static_cast<std::size_t>(std::round(
                clampf(spec.p1, 0.0f, 100.0f) * static_cast<float>(RATE) / 1000.0f
            ));
            for (int ch = 0; ch < CHANNELS; ++ch) {
                if (module.fun_dsp.predelay[ch].size() != samples) {
                    module.fun_dsp.predelay[ch].assign(samples, 0.0f);
                    module.fun_dsp.predelay_index[ch] = 0;
                }
            }
        }
        if (spec.type == "delay") {
            const std::size_t samples = static_cast<std::size_t>(std::max(
                1.0f,
                std::round(clampf(spec.p1, 10.0f, 500.0f) * static_cast<float>(RATE) / 1000.0f)
            ));
            for (int ch = 0; ch < CHANNELS; ++ch) {
                if (module.delay_buffer[ch].size() != samples) {
                    module.delay_buffer[ch].assign(samples, 0.0f);
                    module.delay_index[ch] = 0;
                }
            }
        }
    }
}

void reset_custom_fx_dsp(CustomFxChainDsp& dsp, const MicroState& state) {
    dsp.modules.clear();
    dsp.modules.resize(state.custom_fx_modules.size());
    for (std::size_t i = 0; i < state.custom_fx_modules.size(); ++i) {
        dsp.modules[i].reset(state.custom_fx_modules[i].type);
    }
    sync_custom_fx_dsp(dsp, state);
}

void apply_custom_fx(std::vector<float>& frames, CustomFxChainDsp& dsp, const MicroState& state) {
    if (frames.empty()) return;
    const bool enabled = state.custom_fx_enabled && !state.custom_fx_modules.empty();
    if (enabled && !dsp.active_last) reset_custom_fx_dsp(dsp, state);
    sync_custom_fx_dsp(dsp, state);

    const float target_wet = enabled ? clampf(state.custom_fx_wet, 0.0f, 100.0f) / 100.0f : 0.0f;
    if (target_wet <= 0.0001f && dsp.wet_current <= 0.0001f) {
        dsp.wet_current = 0.0f;
        dsp.active_last = enabled;
        return;
    }

    const std::vector<float> dry = frames;
    std::vector<float> wet = frames;
    const int frame_count = static_cast<int>(frames.size()) / CHANNELS;

    if (enabled) {
        for (std::size_t module_index = 0; module_index < state.custom_fx_modules.size(); ++module_index) {
            const auto& spec = state.custom_fx_modules[module_index];
            auto& module = dsp.modules[module_index];
            if (spec.type == "gain") {
                const float gain = db_to_gain(clampf(spec.p1, -24.0f, 24.0f));
                for (float& sample : wet) sample = std::isfinite(sample) ? clampf(sample * gain, -1.5f, 1.5f) : 0.0f;
            } else if (spec.type == "highpass") {
                for (int frame = 0; frame < frame_count; ++frame) {
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        const int index = frame * CHANNELS + ch;
                        wet[index] = voice_fx_highpass(wet[index], spec.p1, module.hp_prev_in[ch], module.hp_prev_out[ch]);
                    }
                }
            } else if (spec.type == "lowpass") {
                for (int frame = 0; frame < frame_count; ++frame) {
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        const int index = frame * CHANNELS + ch;
                        wet[index] = voice_fx_lowpass(wet[index], spec.p1, module.lp_state[ch]);
                    }
                }
            } else if (spec.type == "saturation") {
                const float drive = clampf(spec.p1, 1.0f, 10.0f);
                const float norm = std::max(0.0001f, std::tanh(drive));
                for (float& sample : wet) {
                    const float safe = std::isfinite(sample) ? sample : 0.0f;
                    sample = clampf(std::tanh(safe * drive) / norm, -1.0f, 1.0f);
                }
            } else if (spec.type == "distortion") {
                const float drive = clampf(spec.p1, 1.0f, 20.0f);
                const float mix = clampf(spec.p2, 0.0f, 100.0f) / 100.0f;
                const float norm = std::max(0.0001f, std::tanh(drive));
                for (float& sample : wet) {
                    const float dry_sample = std::isfinite(sample) ? sample : 0.0f;
                    const float clipped = clampf(std::tanh(dry_sample * drive) / norm, -1.0f, 1.0f);
                    sample = clampf(dry_sample + ((clipped - dry_sample) * mix), -1.2f, 1.2f);
                }
            } else if (spec.type == "pitch") {
                auto& pitch = module.fun_dsp;
                const float semitones = clampf(spec.p1, -12.0f, 12.0f);
                const float factor = std::pow(2.0f, semitones / 12.0f);
                const float mix = clampf(spec.p2, 0.0f, 100.0f) / 100.0f;
                constexpr float pi = 3.14159265358979323846f;
                constexpr float window = 1800.0f;
                constexpr float min_delay = 64.0f;
                const float phase_step = std::fabs(1.0f - factor) / window;
                for (int frame = 0; frame < frame_count; ++frame) {
                    const float phase_a = pitch.pitch_phase;
                    float phase_b = phase_a + 0.5f;
                    if (phase_b >= 1.0f) phase_b -= 1.0f;
                    const auto delay_for = [&](float phase) {
                        return min_delay + (factor > 1.0f ? (1.0f - phase) : phase) * window;
                    };
                    const float weight_a = 0.5f - 0.5f * std::cos(2.0f * pi * phase_a);
                    const float weight_b = 1.0f - weight_a;
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        const int index = frame * CHANNELS + ch;
                        auto& buffer = pitch.pitch_buffer[ch];
                        auto& write = pitch.pitch_write[ch];
                        if (buffer.empty()) buffer.assign(4096, 0.0f);
                        const float input = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                        buffer[write] = input;
                        if (std::fabs(semitones) > 0.0001f && mix > 0.0001f) {
                            const float a = voice_fx_pitch_read(buffer, write, delay_for(phase_a));
                            const float b = voice_fx_pitch_read(buffer, write, delay_for(phase_b));
                            const float shifted = clampf((a * weight_a + b * weight_b) * 1.08f, -1.0f, 1.0f);
                            wet[index] = clampf(input + ((shifted - input) * mix), -1.2f, 1.2f);
                        }
                        write = (write + 1) % buffer.size();
                    }
                    pitch.pitch_phase += phase_step;
                    if (pitch.pitch_phase >= 1.0f) pitch.pitch_phase -= 1.0f;
                }
            } else if (spec.type == "reverb") {
                auto& room = module.fun_dsp;
                const float decay = clampf(spec.p2, 0.0f, 100.0f) / 100.0f;
                const float mix = clampf(spec.p3, 0.0f, 100.0f) / 100.0f;
                const float feedback = 0.40f + (decay * 0.46f);
                for (int frame = 0; frame < frame_count; ++frame) {
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        const int index = frame * CHANNELS + ch;
                        const float direct = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                        const float input = voice_fx_highpass(direct, 110.0f, room.hp_prev_in[ch], room.hp_prev_out[ch]);
                        float predelayed = input;
                        if (!room.predelay[ch].empty()) {
                            auto& buffer = room.predelay[ch];
                            auto& cursor = room.predelay_index[ch];
                            predelayed = buffer[cursor];
                            buffer[cursor] = input;
                            cursor = (cursor + 1) % buffer.size();
                        }
                        const float a = voice_fx_comb(predelayed, room.comb_a[ch], room.comb_a_index[ch], feedback, 0.36f, room.reverb_damp_a[ch]);
                        const float b = voice_fx_comb(predelayed, room.comb_b[ch], room.comb_b_index[ch], feedback * 0.95f, 0.39f, room.reverb_damp_b[ch]);
                        const float c = voice_fx_comb(predelayed, room.comb_c[ch], room.comb_c_index[ch], feedback * 0.90f, 0.42f, room.reverb_damp_c[ch]);
                        const float d = voice_fx_comb(predelayed, room.comb_d[ch], room.comb_d_index[ch], feedback * 0.85f, 0.45f, room.reverb_damp_d[ch]);
                        float tail = (a + b + c + d) * 0.25f;
                        tail = voice_fx_allpass(tail, room.allpass_a[ch], room.allpass_a_index[ch], 0.55f);
                        tail = voice_fx_allpass(tail, room.allpass_b[ch], room.allpass_b_index[ch], 0.48f);
                        const float room_wet = clampf((predelayed * 0.18f) + (tail * 0.82f), -1.0f, 1.0f);
                        wet[index] = clampf(direct + ((room_wet - direct) * mix), -1.2f, 1.2f);
                    }
                }
            } else if (spec.type == "delay") {
                const float feedback = clampf(spec.p2, 0.0f, 85.0f) / 100.0f;
                const float mix = clampf(spec.p3, 0.0f, 100.0f) / 100.0f;
                for (int frame = 0; frame < frame_count; ++frame) {
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        const int index = frame * CHANNELS + ch;
                        auto& buffer = module.delay_buffer[ch];
                        if (buffer.empty()) continue;
                        auto& cursor = module.delay_index[ch];
                        const float input = std::isfinite(wet[index]) ? wet[index] : 0.0f;
                        const float delayed = buffer[cursor];
                        buffer[cursor] = clampf(input + delayed * feedback, -1.2f, 1.2f);
                        cursor = (cursor + 1) % buffer.size();
                        wet[index] = clampf((input * (1.0f - mix)) + (delayed * mix), -1.2f, 1.2f);
                    }
                }
            }
        }
    }

    const float start_wet = clampf(dsp.wet_current, 0.0f, 1.0f);
    const float end_wet = clampf(target_wet, 0.0f, 1.0f);
    for (int frame = 0; frame < frame_count; ++frame) {
        const float progress = static_cast<float>(frame + 1) / static_cast<float>(std::max(1, frame_count));
        const float mix = start_wet + ((end_wet - start_wet) * progress);
        for (int ch = 0; ch < CHANNELS; ++ch) {
            const int index = frame * CHANNELS + ch;
            const float dry_sample = std::isfinite(dry[index]) ? dry[index] : 0.0f;
            const float wet_sample = std::isfinite(wet[index]) ? wet[index] : 0.0f;
            frames[index] = clampf(dry_sample + ((wet_sample - dry_sample) * mix), -1.2f, 1.2f);
        }
    }
    dsp.wet_current = end_wet;
    dsp.active_last = enabled;
    if (!enabled && end_wet <= 0.0001f) {
        for (auto& module : dsp.modules) {
            if (module.type == "delay") {
                for (auto& buffer : module.delay_buffer) std::fill(buffer.begin(), buffer.end(), 0.0f);
            }
            if (module.type == "pitch") module.fun_dsp.reset("pitch_up");
            if (module.type == "reverb") module.fun_dsp.reset("reverb");
        }
    }
}


} // namespace

struct Processor::Impl {
    VoiceFxChainDsp preset_dsp;
    CustomFxChainDsp custom_dsp;
};

Processor::Processor() : impl_(std::make_unique<Impl>()) {}
Processor::~Processor() = default;
Processor::Processor(Processor&&) noexcept = default;
Processor& Processor::operator=(Processor&&) noexcept = default;

void Processor::reset() {
    impl_ = std::make_unique<Impl>();
}

void Processor::process(
    std::vector<float>& frames,
    const std::vector<std::string>& active_effects,
    float amount_percent
) {
    MicroState state;
    state.voice_fx_active = active_effects;
    state.voice_fx_amount = amount_percent;
    apply_voice_fx(frames, impl_->preset_dsp, state);
}

void Processor::process_custom(
    std::vector<float>& frames,
    bool enabled,
    float wet_percent,
    const std::vector<CustomModuleSpec>& modules
) {
    MicroState state;
    state.custom_fx_enabled = enabled;
    state.custom_fx_wet = wet_percent;
    state.custom_fx_modules = modules;
    apply_custom_fx(frames, impl_->custom_dsp, state);
}

} // namespace ksound::voice_fx
