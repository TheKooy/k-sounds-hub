#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <fstream>
#include <iostream>
#include <map>
#include <limits>
#include <set>
#include <sstream>
#include <string>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>

#include "voice_fx_dsp.hpp"
#include "micro_tune_dsp.hpp"
#include "hard_gate_dsp.hpp"

namespace {
constexpr int RATE = 48000;
constexpr int CHANNELS = 2;
constexpr int CHUNK_FRAMES = 480; // 10 ms @ 48 kHz
constexpr int SAMPLE_BYTES = 4;
constexpr int CHUNK_SAMPLES = CHUNK_FRAMES * CHANNELS;
constexpr int CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_BYTES;
constexpr int FRAME_BYTES = CHANNELS * SAMPLE_BYTES;
constexpr int MAX_CAPTURE_BUFFER_BYTES = CHUNK_BYTES * 4;
constexpr std::size_t MAX_DRAIN_BYTES = RATE * CHANNELS * SAMPLE_BYTES * 10;
constexpr int PLAYBACK_WRITE_BUDGET_MS = 3;
constexpr int PLAYBACK_STALL_RESTART_MS = 750;
constexpr float MAX_OUTPUT = 0.98f;

volatile std::sig_atomic_t g_running = 1;

void signal_handler(int) {
    g_running = 0;
}

float clampf(float value, float low, float high) {
    return std::max(low, std::min(high, value));
}

float volume_percent_to_gain(float percent) {
    percent = clampf(percent, 0.0f, 150.0f);
    if (percent <= 100.0f) {
        const float normalized = percent / 100.0f;
        return normalized * normalized;
    }
    return 1.0f + ((percent - 100.0f) / 100.0f);
}

std::vector<std::string> split_tab(const std::string& line) {
    std::vector<std::string> out;
    std::string current;
    for (char c : line) {
        if (c == '\t') {
            out.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    out.push_back(current);
    return out;
}

bool unsafe_primary_micro_source(const std::string& source_name) {
    constexpr const char* monitor_suffix = ".monitor";
    constexpr std::size_t monitor_suffix_size = 8;

    if (source_name.empty() || source_name == "micro") {
        return true;
    }

    return source_name.size() >= monitor_suffix_size
        && source_name.compare(
            source_name.size() - monitor_suffix_size,
            monitor_suffix_size,
            monitor_suffix
        ) == 0;
}

struct ChildProcess {
    pid_t pid{-1};
    int fd{-1};
    bool write_mode{false};

    bool running() const {
        if (pid <= 0) {
            return false;
        }
        int status = 0;
        pid_t res = waitpid(pid, &status, WNOHANG);
        return res == 0;
    }

    void stop() {
        if (fd >= 0) {
            ::close(fd);
            fd = -1;
        }

        if (pid > 0) {
            if (running()) {
                ::kill(pid, SIGTERM);
                for (int i = 0; i < 20; ++i) {
                    if (!running()) {
                        break;
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(20));
                }
                if (running()) {
                    ::kill(pid, SIGKILL);
                }
            }
            int status = 0;
            waitpid(pid, &status, WNOHANG);
            pid = -1;
        }
    }
};

ChildProcess spawn_process(const std::vector<std::string>& args, bool write_mode) {
    int pipefd[2];
    if (pipe(pipefd) != 0) {
        return {};
    }

    pid_t pid = fork();
    if (pid < 0) {
        ::close(pipefd[0]);
        ::close(pipefd[1]);
        return {};
    }

    if (pid == 0) {
        if (write_mode) {
            dup2(pipefd[0], STDIN_FILENO);
        } else {
            dup2(pipefd[1], STDOUT_FILENO);
        }

        int devnull = ::open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDERR_FILENO);
            ::close(devnull);
        }

        ::close(pipefd[0]);
        ::close(pipefd[1]);

        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (const auto& arg : args) {
            argv.push_back(const_cast<char*>(arg.c_str()));
        }
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    ChildProcess proc;
    proc.pid = pid;
    proc.write_mode = write_mode;

    if (write_mode) {
        ::close(pipefd[0]);
        proc.fd = pipefd[1];
    } else {
        ::close(pipefd[1]);
        proc.fd = pipefd[0];
    }

    // Both directions must be non-blocking. In particular, a slow pacat must
    // never stall capture and turn a one-second event cadence into four seconds.
    int flags = fcntl(proc.fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(proc.fd, F_SETFL, flags | O_NONBLOCK);
    }

    return proc;
}

struct SourceSpec {
    std::string key;
    std::string source_name;
    float gain{1.0f};
};

struct MicroState {
    bool enabled{true};
    bool muted{false};
    float volume_percent{100.0f};
    std::string mic_source;
    bool hard_gate_enabled{false};
    float hard_gate_threshold{12.0f};
    bool processing_enabled{false};
    float input_gain_db{0.0f};
    bool noise_suppression_enabled{false};
    float noise_suppression{0.0f};
    bool gate_enabled{false};
    float gate_threshold{35.0f};
    std::vector<ksound::micro_tune::EqBandSpec> eq_bands;
    bool compressor_enabled{false};
    float compressor_amount{45.0f};
    bool deesser_enabled{false};
    float deesser_amount{35.0f};
    bool limiter_enabled{true};
    float limiter_ceiling_db{-1.0f};
    bool voice_fx_enabled{false};
    std::string voice_fx_preset{"radio"}; // IP13a compatibility mirror
    std::vector<std::string> voice_fx_active;
    float voice_fx_amount{72.0f};
    bool custom_fx_enabled{false};
    float custom_fx_wet{100.0f};
    std::vector<ksound::voice_fx::CustomModuleSpec> custom_fx_modules;
    std::vector<SourceSpec> sends;

    bool native_monitor_enabled{false}; // final MICRO mix -> retour; no virtual micro recapture
    bool return_enabled{false};
    bool return_muted{false};
    float return_volume_percent{100.0f};
    std::string return_target_sink{"retour"};
    std::vector<SourceSpec> return_sources;
};

std::vector<ksound::micro_tune::EqBandSpec> parse_eq_bands(const std::string& value) {
    std::vector<ksound::micro_tune::EqBandSpec> bands;
    std::stringstream entries(value);
    std::string item;
    while (std::getline(entries, item, ',')) {
        if (item.empty()) continue;
        std::stringstream fields(item);
        std::string frequency, gain, q;
        if (!std::getline(fields, frequency, ':') || !std::getline(fields, gain, ':') || !std::getline(fields, q, ':')) continue;
        try {
            ksound::micro_tune::EqBandSpec band;
            band.frequency = clampf(std::stof(frequency), 20.0f, 20000.0f);
            band.gain_db = clampf(std::stof(gain), -12.0f, 12.0f);
            band.q = clampf(std::stof(q), 0.1f, 10.0f);
            bands.push_back(band);
        } catch (...) {}
    }
    return bands;
}

ksound::micro_tune::Settings micro_tune_settings(const MicroState& state) {
    ksound::micro_tune::Settings settings;
    settings.processing_enabled = state.processing_enabled;
    settings.input_gain_db = state.input_gain_db;
    settings.noise_suppression_enabled = state.noise_suppression_enabled;
    settings.noise_suppression = state.noise_suppression;
    settings.gate_enabled = state.gate_enabled;
    settings.gate_threshold = state.gate_threshold;
    settings.eq_bands = state.eq_bands;
    settings.compressor_enabled = state.compressor_enabled;
    settings.compressor_amount = state.compressor_amount;
    settings.deesser_enabled = state.deesser_enabled;
    settings.deesser_amount = state.deesser_amount;
    settings.limiter_enabled = state.limiter_enabled;
    settings.limiter_ceiling_db = state.limiter_ceiling_db;
    return settings;
}

ksound::hard_gate::Settings hard_gate_settings(const MicroState& state) {
    ksound::hard_gate::Settings settings;
    settings.enabled = state.hard_gate_enabled;
    settings.threshold_percent = state.hard_gate_threshold;
    settings.volume_percent = state.volume_percent;
    return settings;
}

struct CaptureClient {
    std::string source_name;
    ChildProcess proc;
    std::vector<char> buffer;
    std::uint64_t short_reads{0};
    std::uint64_t stale_trims{0};
    // Frames remaining in our userspace capture queue AFTER consumption.
    // Excludes the USB hardware and PipeWire/parec buffers.
    std::size_t max_queued_after_read_frames{0};

    void stop() {
        proc.stop();
        buffer.clear();
    }

    void ensure_started() {
        if (proc.running()) {
            return;
        }

        stop();

        proc = spawn_process(
            {
                "parec",
                "--device=" + source_name,
                "--raw",
                "--format=float32le",
                "--rate=48000",
                "--channels=2",
                "--latency-msec=10",
            },
            false
        );
    }

    std::vector<float> read_chunk() {
        ensure_started();

        std::vector<float> silence(CHUNK_SAMPLES, 0.0f);
        if (!proc.running() || proc.fd < 0) {
            return silence;
        }

        bool stale_data_dropped = false;
        auto append_recent = [&](const char* data, std::size_t size) {
            buffer.insert(buffer.end(), data, data + size);
            if (buffer.size() <= static_cast<std::size_t>(MAX_CAPTURE_BUFFER_BYTES)) {
                return;
            }

            std::size_t drop = buffer.size() - static_cast<std::size_t>(MAX_CAPTURE_BUFFER_BYTES);
            const std::size_t remainder = drop % static_cast<std::size_t>(FRAME_BYTES);
            if (remainder != 0) {
                drop += static_cast<std::size_t>(FRAME_BYTES) - remainder;
            }
            buffer.erase(
                buffer.begin(),
                buffer.begin() + static_cast<std::ptrdiff_t>(drop)
            );
            ++stale_trims;
            stale_data_dropped = true;
        };

        auto read_available = [&]() -> bool {
            std::size_t drained = 0;
            for (;;) {
                char tmp[65536];
                ssize_t n = ::read(proc.fd, tmp, sizeof(tmp));
                if (n > 0) {
                    const auto size = static_cast<std::size_t>(n);
                    drained += size;
                    append_recent(tmp, size);
                    if (drained >= MAX_DRAIN_BYTES) {
                        // parec may have an even larger queue upstream. Respawn
                        // it instead of allowing old audio to re-enter the mix.
                        stop();
                        return false;
                    }
                    continue;
                }
                if (n < 0 && errno == EINTR) {
                    continue;
                }
                if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                    return true;
                }
                return false;
            }
            return true;
        };

        // Wait briefly for a complete 10 ms chunk instead of returning silence
        // too often. Returning repeated silence chunks is a common source of
        // small crackles in this parec -> engine -> pacat pipeline.
        for (int attempt = 0; attempt < 6 && static_cast<int>(buffer.size()) < CHUNK_BYTES; ++attempt) {
            if (!read_available()) {
                stop();
                return silence;
            }

            if (static_cast<int>(buffer.size()) >= CHUNK_BYTES) {
                break;
            }

            pollfd pfd{};
            pfd.fd = proc.fd;
            pfd.events = POLLIN;
            const int poll_result = ::poll(&pfd, 1, 2);
            if (poll_result < 0 && errno == EINTR) {
                continue;
            }
            if (poll_result <= 0) {
                break;
            }
        }

        if (static_cast<int>(buffer.size()) < CHUNK_BYTES) {
            ++short_reads;
            return silence;
        }

        // If the rolling limit was needed, resynchronize directly to the most
        // recent complete chunk. Normal two/three-chunk PipeWire bursts remain
        // continuous and are consumed in order.
        if (stale_data_dropped && static_cast<int>(buffer.size()) > CHUNK_BYTES) {
            const std::size_t complete_bytes =
                (buffer.size() / static_cast<std::size_t>(FRAME_BYTES))
                * static_cast<std::size_t>(FRAME_BYTES);
            const std::size_t start = complete_bytes - static_cast<std::size_t>(CHUNK_BYTES);
            buffer.erase(
                buffer.begin(),
                buffer.begin() + static_cast<std::ptrdiff_t>(start)
            );
        }

        std::vector<float> out(CHUNK_SAMPLES, 0.0f);
        std::memcpy(out.data(), buffer.data(), CHUNK_BYTES);
        buffer.erase(buffer.begin(), buffer.begin() + CHUNK_BYTES);
        max_queued_after_read_frames = std::max(
            max_queued_after_read_frames,
            buffer.size() / static_cast<std::size_t>(FRAME_BYTES)
        );
        return out;
    }
};

struct PlaybackClient {
    std::string sink_name;
    ChildProcess proc;
    std::chrono::steady_clock::time_point blocked_since{};
    std::uint64_t backpressure_drops{0};

    void set_sink(std::string wanted) {
        if (wanted.empty()) {
            wanted = "micro_bus";
        }
        if (sink_name == wanted) {
            return;
        }
        sink_name = wanted;
        stop();
    }

    void stop() {
        proc.stop();
        blocked_since = {};
    }

    bool backpressure_timed_out() {
        const auto now = std::chrono::steady_clock::now();
        if (blocked_since.time_since_epoch().count() == 0) {
            blocked_since = now;
            return false;
        }
        return now - blocked_since >= std::chrono::milliseconds(PLAYBACK_STALL_RESTART_MS);
    }

    bool ensure_started() {
        if (sink_name.empty()) {
            return false;
        }

        if (proc.running() && proc.fd >= 0) {
            return true;
        }

        stop();

        proc = spawn_process(
            {
                "pacat",
                "--playback",
                "--device=" + sink_name,
                "--raw",
                "--format=float32le",
                "--rate=48000",
                "--channels=2",
                "--latency-msec=20",
                "--process-time-msec=10",
            },
            true
        );

        if (!proc.running() || proc.fd < 0) {
            proc = spawn_process(
                {
                    "pw-cat",
                    "--playback",
                    "--target",
                    sink_name,
                    "--rate",
                    "48000",
                    "--channels",
                    "2",
                    "--format",
                    "f32",
                },
                true
            );
        }

        return proc.running() && proc.fd >= 0;
    }

    static void sanitize_in_place(std::vector<float>& safe) {
        float peak = 0.0f;
        for (float sample : safe) {
            if (std::isfinite(sample)) {
                peak = std::max(peak, std::fabs(sample));
            }
        }

        if (peak > MAX_OUTPUT) {
            const float scale = MAX_OUTPUT / peak;
            for (float& sample : safe) {
                sample = std::isfinite(sample) ? sample * scale : 0.0f;
            }
        } else {
            for (float& sample : safe) {
                sample = std::isfinite(sample) ? sample : 0.0f;
            }
        }

    }

    void write(const std::vector<float>& frames) {
        if (!ensure_started()) {
            return;
        }
        std::vector<float> safe = frames;
        sanitize_in_place(safe);
        write_prepared_to_running_client(safe);
    }

    // The MICRO export and MIC OUT monitor consume the SAME prepared frames.
    // No extra copy, peak scan or limiter pass for the second destination.
    void write_prepared(const std::vector<float>& safe) {
        if (!ensure_started()) {
            return;
        }
        write_prepared_to_running_client(safe);
    }

    void write_prepared_to_running_client(const std::vector<float>& safe) {
        const char* data = reinterpret_cast<const char*>(safe.data());
        std::size_t total = safe.size() * sizeof(float);
        std::size_t done = 0;
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::milliseconds(PLAYBACK_WRITE_BUDGET_MS);

        while (done < total) {
            ssize_t n = ::write(proc.fd, data + done, total - done);
            if (n > 0) {
                done += static_cast<std::size_t>(n);
                blocked_since = {};
                continue;
            }
            if (n < 0 && errno == EINTR) {
                continue;
            }

            if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                if (std::chrono::steady_clock::now() >= deadline) {
                    // Keep the healthy playback client alive. Restarting pacat
                    // on ordinary transient backpressure can create a permanent
                    // start/fill/kill loop where micro_bus never receives audio.
                    // Drop this late chunk and retry on the next 10 ms tick.
                    // Only a sustained 750 ms stall warrants a real restart.
                    ++backpressure_drops;
                    if (backpressure_timed_out()) {
                        stop();
                    }
                    return;
                }
                pollfd pfd{};
                pfd.fd = proc.fd;
                pfd.events = POLLOUT;
                const int ready = ::poll(&pfd, 1, PLAYBACK_WRITE_BUDGET_MS);
                if (ready <= 0) {
                    // A full pipe is not a crashed child. Leave it running so
                    // pacat can drain normally without an audible route reset.
                    ++backpressure_drops;
                    if (backpressure_timed_out()) {
                        stop();
                    }
                    return;
                }
                continue;
            }

            stop();
            return;
        }
    }
};

MicroState parse_state_file(const std::string& path) {
    MicroState state;
    std::ifstream in(path);
    if (!in) {
        return state;
    }

    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) {
            continue;
        }

        auto parts = split_tab(line);
        if (parts.empty()) {
            continue;
        }

        if (parts[0] == "enabled" && parts.size() >= 2) {
            state.enabled = parts[1] == "1";
        } else if (parts[0] == "muted" && parts.size() >= 2) {
            state.muted = parts[1] == "1";
        } else if (parts[0] == "volume" && parts.size() >= 2) {
            try {
                state.volume_percent = std::stof(parts[1]);
            } catch (...) {
                state.volume_percent = 100.0f;
            }
        } else if (parts[0] == "source" && parts.size() >= 2) {
            state.mic_source = parts[1];
        } else if (parts[0] == "hard_gate_enabled" && parts.size() >= 2) {
            state.hard_gate_enabled = parts[1] == "1";
        } else if (parts[0] == "hard_gate_threshold" && parts.size() >= 2) {
            try {
                state.hard_gate_threshold = std::stof(parts[1]);
            } catch (...) {
                state.hard_gate_threshold = 12.0f;
            }
        } else if (parts[0] == "processing_enabled" && parts.size() >= 2) {
            state.processing_enabled = parts[1] == "1";
        } else if (parts[0] == "input_gain_db" && parts.size() >= 2) {
            try { state.input_gain_db = std::stof(parts[1]); } catch (...) { state.input_gain_db = 0.0f; }
        } else if (parts[0] == "noise_suppression_enabled" && parts.size() >= 2) {
            state.noise_suppression_enabled = parts[1] == "1";
        } else if (parts[0] == "noise_suppression" && parts.size() >= 2) {
            try { state.noise_suppression = std::stof(parts[1]); } catch (...) { state.noise_suppression = 0.0f; }
        } else if (parts[0] == "gate_enabled" && parts.size() >= 2) {
            state.gate_enabled = parts[1] == "1";
        } else if (parts[0] == "gate_threshold" && parts.size() >= 2) {
            try { state.gate_threshold = std::stof(parts[1]); } catch (...) { state.gate_threshold = 35.0f; }
        } else if (parts[0] == "eq_bands" && parts.size() >= 2) {
            state.eq_bands = parse_eq_bands(parts[1]);
        } else if (parts[0] == "compressor_enabled" && parts.size() >= 2) {
            state.compressor_enabled = parts[1] == "1";
        } else if (parts[0] == "compressor_amount" && parts.size() >= 2) {
            try { state.compressor_amount = std::stof(parts[1]); } catch (...) { state.compressor_amount = 45.0f; }
        } else if (parts[0] == "deesser_enabled" && parts.size() >= 2) {
            state.deesser_enabled = parts[1] == "1";
        } else if (parts[0] == "deesser_amount" && parts.size() >= 2) {
            try { state.deesser_amount = std::stof(parts[1]); } catch (...) { state.deesser_amount = 35.0f; }
        } else if (parts[0] == "limiter_enabled" && parts.size() >= 2) {
            state.limiter_enabled = parts[1] == "1";
        } else if (parts[0] == "limiter_ceiling_db" && parts.size() >= 2) {
            try { state.limiter_ceiling_db = std::stof(parts[1]); } catch (...) { state.limiter_ceiling_db = -1.0f; }
        } else if (parts[0] == "voice_fx_enabled" && parts.size() >= 2) {
            state.voice_fx_enabled = parts[1] == "1";
        } else if (parts[0] == "voice_fx_preset" && parts.size() >= 2) {
            state.voice_fx_preset = parts[1];
        } else if (parts[0] == "voice_fx_active" && parts.size() >= 2) {
            state.voice_fx_active.clear();
            std::stringstream active(parts[1]);
            std::string effect;
            while (std::getline(active, effect, ',')) {
                if (!effect.empty()) state.voice_fx_active.push_back(effect);
            }
        } else if (parts[0] == "voice_fx_amount" && parts.size() >= 2) {
            try { state.voice_fx_amount = std::stof(parts[1]); } catch (...) { state.voice_fx_amount = 72.0f; }
        } else if (parts[0] == "custom_fx_enabled" && parts.size() >= 2) {
            state.custom_fx_enabled = parts[1] == "1";
        } else if (parts[0] == "custom_fx_wet" && parts.size() >= 2) {
            try { state.custom_fx_wet = std::stof(parts[1]); } catch (...) { state.custom_fx_wet = 100.0f; }
        } else if (parts[0] == "custom_fx_module" && parts.size() >= 3) {
            if (state.custom_fx_modules.size() >= 16) continue;
            ksound::voice_fx::CustomModuleSpec spec;
            spec.type = parts[1];
            try { spec.p1 = std::stof(parts[2]); } catch (...) { spec.p1 = 0.0f; }
            if (parts.size() >= 4) { try { spec.p2 = std::stof(parts[3]); } catch (...) { spec.p2 = 0.0f; } }
            if (parts.size() >= 5) { try { spec.p3 = std::stof(parts[4]); } catch (...) { spec.p3 = 0.0f; } }
            state.custom_fx_modules.push_back(spec);
        } else if (parts[0] == "send" && parts.size() >= 4) {
            if (parts[2] != "1") {
                continue;
            }

            SourceSpec spec;
            spec.key = parts[1];
            spec.source_name = parts[3];

            if (parts.size() >= 5) {
                try {
                    spec.gain = std::stof(parts[4]);
                } catch (...) {
                    spec.gain = 1.0f;
                }
            }

            if (!spec.source_name.empty()) {
                state.sends.push_back(spec);
            }
        } else if (parts[0] == "native_monitor" && parts.size() >= 2) {
            state.native_monitor_enabled = parts[1] == "1";
        } else if (parts[0] == "return_enabled" && parts.size() >= 2) {
            state.return_enabled = parts[1] == "1";
        } else if (parts[0] == "return_muted" && parts.size() >= 2) {
            state.return_muted = parts[1] == "1";
        } else if (parts[0] == "return_volume" && parts.size() >= 2) {
            try {
                state.return_volume_percent = std::stof(parts[1]);
            } catch (...) {
                state.return_volume_percent = 100.0f;
            }
        } else if (parts[0] == "return_target" && parts.size() >= 2) {
            state.return_target_sink = parts[1].empty() ? "retour" : parts[1];
        } else if (parts[0] == "return_source" && parts.size() >= 4) {
            if (parts[2] != "1") {
                continue;
            }

            SourceSpec spec;
            spec.key = parts[1];
            spec.source_name = parts[3];

            if (parts.size() >= 5) {
                try {
                    spec.gain = std::stof(parts[4]);
                } catch (...) {
                    spec.gain = 1.0f;
                }
            }

            if (!spec.source_name.empty()) {
                state.return_sources.push_back(spec);
            }
        }
    }

    state.volume_percent = clampf(state.volume_percent, 0.0f, 150.0f);
    state.hard_gate_threshold = clampf(state.hard_gate_threshold, 0.0f, 100.0f);
    state.input_gain_db = clampf(state.input_gain_db, -12.0f, 24.0f);
    state.noise_suppression = clampf(state.noise_suppression, 0.0f, 100.0f);
    state.gate_threshold = clampf(state.gate_threshold, 0.0f, 100.0f);
    state.compressor_amount = clampf(state.compressor_amount, 0.0f, 100.0f);
    state.deesser_amount = clampf(state.deesser_amount, 0.0f, 100.0f);
    state.limiter_ceiling_db = clampf(state.limiter_ceiling_db, -12.0f, 0.0f);
    state.voice_fx_amount = clampf(state.voice_fx_amount, 0.0f, 100.0f);
    const std::array<std::string, 9> voice_fx_order = {"distortion", "pitch_down", "pitch_up", "radio", "telephone", "robot", "reverb", "church", "church_pa"};
    if (std::find(voice_fx_order.begin(), voice_fx_order.end(), state.voice_fx_preset) == voice_fx_order.end()) {
        state.voice_fx_preset = "radio";
    }
    std::vector<std::string> sanitized_voice_fx;
    for (const auto& wanted : voice_fx_order) {
        if (std::find(state.voice_fx_active.begin(), state.voice_fx_active.end(), wanted) != state.voice_fx_active.end()) {
            sanitized_voice_fx.push_back(wanted);
        }
    }
    // Keep incompatible pairs deterministic even for hand-edited state files.
    if (std::find(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "radio") != sanitized_voice_fx.end() &&
        std::find(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "telephone") != sanitized_voice_fx.end()) {
        sanitized_voice_fx.erase(std::remove(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "telephone"), sanitized_voice_fx.end());
    }
    if (std::find(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "pitch_down") != sanitized_voice_fx.end() &&
        std::find(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "pitch_up") != sanitized_voice_fx.end()) {
        sanitized_voice_fx.erase(std::remove(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), "pitch_up"), sanitized_voice_fx.end());
    }
    // Only one room model at once: compact Reverb or one of the Church variants.
    bool room_kept = false;
    for (const auto& room : {std::string("reverb"), std::string("church"), std::string("church_pa")}) {
        auto it = std::find(sanitized_voice_fx.begin(), sanitized_voice_fx.end(), room);
        if (it == sanitized_voice_fx.end()) continue;
        if (!room_kept) { room_kept = true; continue; }
        sanitized_voice_fx.erase(it);
    }
    // IP13a compatibility: old state files only have enabled+preset.
    if (sanitized_voice_fx.empty() && state.voice_fx_enabled) sanitized_voice_fx.push_back(state.voice_fx_preset);
    state.voice_fx_active = sanitized_voice_fx;
    state.voice_fx_enabled = !state.voice_fx_active.empty();
    state.custom_fx_wet = clampf(state.custom_fx_wet, 0.0f, 100.0f);
    std::vector<ksound::voice_fx::CustomModuleSpec> sanitized_custom_fx;
    sanitized_custom_fx.reserve(std::min<std::size_t>(16, state.custom_fx_modules.size()));
    for (auto spec : state.custom_fx_modules) {
        if (sanitized_custom_fx.size() >= 16) break;
        if (spec.type == "gain") {
            spec.p1 = clampf(spec.p1, -24.0f, 24.0f);
            spec.p2 = spec.p3 = 0.0f;
        } else if (spec.type == "highpass" || spec.type == "lowpass") {
            spec.p1 = clampf(spec.p1, 40.0f, 18000.0f);
            spec.p2 = spec.p3 = 0.0f;
        } else if (spec.type == "saturation") {
            spec.p1 = clampf(spec.p1, 1.0f, 10.0f);
            spec.p2 = spec.p3 = 0.0f;
        } else if (spec.type == "delay") {
            spec.p1 = clampf(spec.p1, 10.0f, 500.0f);
            spec.p2 = clampf(spec.p2, 0.0f, 85.0f);
            spec.p3 = clampf(spec.p3, 0.0f, 100.0f);
        } else if (spec.type == "distortion") {
            spec.p1 = clampf(spec.p1, 1.0f, 20.0f);
            spec.p2 = clampf(spec.p2, 0.0f, 100.0f);
            spec.p3 = 0.0f;
        } else if (spec.type == "pitch") {
            spec.p1 = clampf(spec.p1, -12.0f, 12.0f);
            spec.p2 = clampf(spec.p2, 0.0f, 100.0f);
            spec.p3 = 0.0f;
        } else if (spec.type == "reverb") {
            spec.p1 = clampf(spec.p1, 0.0f, 100.0f);
            spec.p2 = clampf(spec.p2, 0.0f, 100.0f);
            spec.p3 = clampf(spec.p3, 0.0f, 100.0f);
        } else {
            continue;
        }
        sanitized_custom_fx.push_back(spec);
    }
    state.custom_fx_modules = sanitized_custom_fx;
    state.custom_fx_enabled = state.custom_fx_enabled && !state.custom_fx_modules.empty();
    state.return_volume_percent = clampf(state.return_volume_percent, 0.0f, 150.0f);
    if (unsafe_primary_micro_source(state.mic_source)) {
        // Last-resort guard for old or manually edited state files. Capturing
        // the exported "micro" source would feed micro_bus back into itself.
        state.mic_source.clear();
    }
    if (state.return_target_sink.empty()) {
        state.return_target_sink = "retour";
    }
    return state;
}

std::string state_signature(const MicroState& state) {
    std::ostringstream out;
    out << state.enabled << "\n";
    out << state.muted << "\n";
    out << state.volume_percent << "\n";
    out << state.mic_source << "\n";
    out << state.hard_gate_enabled << "\n";
    out << state.hard_gate_threshold << "\n";
    out << state.processing_enabled << "\n";
    out << state.input_gain_db << "\n";
    out << state.noise_suppression_enabled << "\n";
    out << state.noise_suppression << "\n";
    out << state.gate_enabled << "\n";
    out << state.gate_threshold << "\n";
    for (const auto& band : state.eq_bands) {
        out << "eq\t" << band.frequency << '\t' << band.gain_db << '\t' << band.q << "\n";
    }
    out << state.compressor_enabled << "\n";
    out << state.compressor_amount << "\n";
    out << state.deesser_enabled << "\n";
    out << state.deesser_amount << "\n";
    out << state.limiter_enabled << "\n";
    out << state.limiter_ceiling_db << "\n";
    out << state.voice_fx_enabled << "\n";
    out << state.voice_fx_preset << "\n";
    for (const auto& effect : state.voice_fx_active) out << "voice_fx\t" << effect << "\n";
    out << state.voice_fx_amount << "\n";
    out << state.custom_fx_enabled << "\n";
    out << state.custom_fx_wet << "\n";
    for (const auto& module : state.custom_fx_modules) {
        out << "custom_fx\t" << module.type << "\t" << module.p1 << "\t" << module.p2 << "\t" << module.p3 << "\n";
    }
    for (const auto& send : state.sends) {
        out << "send\t" << send.key << "\t" << send.source_name << "\t" << send.gain << "\n";
    }
    out << state.native_monitor_enabled << "\n";
    out << state.return_enabled << "\n";
    out << state.return_muted << "\n";
    out << state.return_volume_percent << "\n";
    out << state.return_target_sink << "\n";
    for (const auto& source : state.return_sources) {
        out << "return\t" << source.key << "\t" << source.source_name << "\t" << source.gain << "\n";
    }
    return out.str();
}

// Glass writes the entire state through a temporary file + atomic rename.
// A stat of that file every tick is substantially cheaper and more predictable
// than parsing the whole EQ/FX state and rebuilding the signature 100 times/s.
// Compare inode and nanosecond mtime/ctime as well as size; no polling interval
// is added, and the first tick following a hot-reload sees the new settings.
struct StateFileCache {
    MicroState state{};
    std::string signature{};
    struct stat stamp{};
    bool known{false};
    bool present{false};
    std::uint64_t reads{0};
    std::uint64_t hits{0};

    void refresh(const std::string& path) {
        struct stat fresh{};
        const bool exists = ::stat(path.c_str(), &fresh) == 0;
        if (known && present == exists && (
            !exists || (
                fresh.st_dev == stamp.st_dev &&
                fresh.st_ino == stamp.st_ino &&
                fresh.st_size == stamp.st_size &&
                fresh.st_mtim.tv_sec == stamp.st_mtim.tv_sec &&
                fresh.st_mtim.tv_nsec == stamp.st_mtim.tv_nsec &&
                fresh.st_ctim.tv_sec == stamp.st_ctim.tv_sec &&
                fresh.st_ctim.tv_nsec == stamp.st_ctim.tv_nsec
            )
        )) {
            ++hits;
            return;
        }
        known = true;
        present = exists;
        if (exists) {
            stamp = fresh;
        }
        // Keep the former missing-file behavior: use safe default MicroState.
        state = parse_state_file(path);
        signature = state_signature(state);
        ++reads;
    }
};

void mix_add(std::vector<float>& mix, const std::vector<float>& chunk, float gain) {
    const float safe_gain = clampf(gain, 0.0f, 2.5f);
    const int count = std::min(static_cast<int>(mix.size()), static_cast<int>(chunk.size()));
    for (int i = 0; i < count; ++i) {
        mix[i] += chunk[i] * safe_gain;
    }
}

void mix_add_ramped(
    std::vector<float>& mix,
    const std::vector<float>& chunk,
    float& current_gain,
    float target_gain
) {
    const float start_gain = clampf(current_gain, 0.0f, 2.5f);
    const float end_gain = clampf(target_gain, 0.0f, 2.5f);
    const int sample_count = std::min(static_cast<int>(mix.size()), static_cast<int>(chunk.size()));
    const int frame_count = std::max(1, sample_count / CHANNELS);

    for (int frame = 0; frame < frame_count; ++frame) {
        const float progress = static_cast<float>(frame + 1) / static_cast<float>(frame_count);
        const float gain = start_gain + ((end_gain - start_gain) * progress);

        for (int channel = 0; channel < CHANNELS; ++channel) {
            const int index = frame * CHANNELS + channel;
            if (index < sample_count) {
                mix[index] += chunk[index] * gain;
            }
        }
    }

    current_gain = end_gain;
}


float peak_level(const std::vector<float>& frames, int channel) {
    float peak = 0.0f;
    for (int i = channel; i < static_cast<int>(frames.size()); i += CHANNELS) {
        const float sample = frames[i];
        if (std::isfinite(sample)) {
            peak = std::max(peak, std::fabs(sample));
        }
    }
    return clampf(peak, 0.0f, 1.0f);
}

void write_levels_file(
    const std::string& path,
    const std::vector<float>& micro_mix,
    const std::vector<float>& return_mix,
    const ksound::micro_tune::Telemetry& processing
) {
    if (path.empty()) {
        return;
    }

    static auto last_write = std::chrono::steady_clock::time_point{};
    const auto now = std::chrono::steady_clock::now();

    if (last_write.time_since_epoch().count() != 0) {
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_write).count();
        if (elapsed < 25) {
            return;
        }
    }

    last_write = now;

    const float micro_l = peak_level(micro_mix, 0);
    const float micro_r = peak_level(micro_mix, 1);
    const float return_l = peak_level(return_mix, 0);
    const float return_r = peak_level(return_mix, 1);

    const std::string tmp_path = path + ".tmp";
    std::ofstream out(tmp_path);
    if (!out) {
        return;
    }

    out << "{"
        << "\"channels\":{"
        << "\"micro\":[" << micro_l << "," << micro_r << "],"
        << "\"return-mic\":[" << return_l << "," << return_r << "]"
        << "},"
        << "\"processing\":{"
        << "\"compressor_reduction_db\":" << processing.compressor_reduction_db << ","
        << "\"deesser_reduction_db\":" << processing.deesser_reduction_db << ","
        << "\"limiter_reduction_db\":" << processing.limiter_reduction_db
        << "}"
        << "}\n";
    out.close();

    std::rename(tmp_path.c_str(), path.c_str());
}

} // namespace

int main(int argc, char** argv) {
    std::signal(SIGTERM, signal_handler);
    std::signal(SIGINT, signal_handler);
    std::signal(SIGPIPE, SIG_IGN);

    std::string state_path;
    std::string log_path;
    std::string levels_path;
    bool probe_rnnoise = false;
    bool probe_micro_tune = false;
    bool probe_hard_gate = false;
    bool probe_soft_gate = false;
    bool probe_processing_telemetry = false;
    bool probe_voice_fx = false;
    bool probe_fun_voice_fx = false;
    bool probe_custom_fx = false;
    bool probe_stable_monitor = false;
    int period_ms = 10;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                return {};
            }
            return argv[++i];
        };

        if (arg == "--state") {
            state_path = next();
        } else if (arg == "--probe-rnnoise") {
            probe_rnnoise = true;
        } else if (arg == "--probe-micro-tune") {
            probe_micro_tune = true;
        } else if (arg == "--probe-hard-gate") {
            probe_hard_gate = true;
        } else if (arg == "--probe-soft-gate") {
            probe_soft_gate = true;
        } else if (arg == "--probe-processing-telemetry") {
            probe_processing_telemetry = true;
        } else if (arg == "--probe-voice-fx") {
            probe_voice_fx = true;
        } else if (arg == "--probe-fun-voice-fx") {
            probe_fun_voice_fx = true;
        } else if (arg == "--probe-custom-fx") {
            probe_custom_fx = true;
        } else if (arg == "--probe-stable-monitor") {
            probe_stable_monitor = true;
        } else if (arg == "--log") {
            log_path = next();
        } else if (arg == "--levels") {
            levels_path = next();
        } else if (arg == "--period-ms") {
            try {
                period_ms = std::max(5, std::min(20, std::stoi(next())));
            } catch (...) {
                period_ms = 10;
            }
        }
    }

    // Offline deterministic check. No PipeWire process or audio device is
    // opened: the test runner swaps its OWN temporary state file atomically.
    if (probe_stable_monitor) {
        if (state_path.empty()) return 2;
        StateFileCache cache;
        cache.refresh(state_path);
        const float first_volume = cache.state.volume_percent;
        const std::string first_signature = cache.signature;
        cache.refresh(state_path);
        if (cache.reads != 1 || cache.hits != 1) return 4;
        std::cout << "probe-ready" << std::endl;
        std::string go;
        if (!std::getline(std::cin, go)) return 5;
        cache.refresh(state_path);
        if (cache.reads != 2 || cache.state.volume_percent == first_volume ||
            cache.signature == first_signature) return 6;
        const float nan = std::numeric_limits<float>::quiet_NaN();
        std::vector<float> original{0.25f, -0.5f, 1.2f, nan};
        auto shared_output = original;
        PlaybackClient::sanitize_in_place(shared_output);
        auto monitor_output = shared_output;
        auto micro_output = shared_output;
        if (micro_output != monitor_output ||
            shared_output.size() != 4 ||
            !std::all_of(shared_output.begin(), shared_output.end(),
                         [](float v) { return std::isfinite(v) && std::fabs(v) <= MAX_OUTPUT; }) ||
            !std::isnan(original[3])) return 7;
        std::cout << "stable_monitor=ok state_reads=" << cache.reads
                  << " cache_hits=" << cache.hits
                  << " identical_feeds=1" << std::endl;
        return 0;
    }

    if (probe_rnnoise) {
        ksound::micro_tune::Processor probe;
        const bool available = probe.rnnoise_available();
        std::cout << (available ? "rnnoise=available\n" : "rnnoise=unavailable\n");
        return available ? 0 : 3;
    }

    if (probe_hard_gate) {
        ksound::hard_gate::Processor gate;
        ksound::hard_gate::Settings settings;
        settings.enabled = true;
        settings.threshold_percent = 65.0f;
        settings.volume_percent = 100.0f;

        double sum_abs = 0.0;
        double weighted = 0.0;
        float peak = 0.0f;
        std::size_t ordinal = 1;

        for (int chunk = 0; chunk < 28; ++chunk) {
            float amplitude = 0.002f;
            if (chunk >= 2 && chunk < 5) amplitude = 0.55f;
            if (chunk >= 23 && chunk < 25) amplitude = 0.50f;
            std::vector<float> frames(CHUNK_SAMPLES, amplitude);
            gate.process(frames, settings);
            for (float sample : frames) {
                const float magnitude = std::fabs(sample);
                sum_abs += magnitude;
                weighted += static_cast<double>(sample) * static_cast<double>((ordinal % 257) + 1);
                peak = std::max(peak, magnitude);
                ++ordinal;
            }
        }

        const bool ok = std::isfinite(sum_abs) && std::isfinite(weighted) && std::isfinite(peak)
            && peak > 0.49f && peak <= 0.56f;
        std::cout << "hard_gate_sum=" << sum_abs << " "
                  << "hard_gate_weighted=" << weighted << " "
                  << "hard_gate_peak=" << peak << " "
                  << "hard_gate_last_peak=" << gate.last_peak() << " "
                  << "hard_gate=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 6;
    }

    if (probe_soft_gate) {
        ksound::micro_tune::Processor gate;
        ksound::micro_tune::Settings settings;
        settings.processing_enabled = true;
        settings.gate_enabled = true;
        settings.gate_threshold = 35.0f;
        std::vector<float> quiet(CHUNK_SAMPLES, 0.0015f);
        std::vector<float> loud(CHUNK_SAMPLES, 0.18f);
        float quiet_tail = 0.0f;
        for (int i = 0; i < 48; ++i) {
            gate.process_post_gate(quiet, settings);
            quiet_tail = std::fabs(quiet.back());
            std::fill(quiet.begin(), quiet.end(), 0.0015f);
        }
        gate.process_post_gate(loud, settings);
        float loud_peak = 0.0f;
        for (float sample : loud) loud_peak = std::max(loud_peak, std::fabs(sample));
        const bool ok = quiet_tail < 0.00020f && loud_peak > 0.12f;
        std::cout << "soft_gate_quiet=" << quiet_tail << " soft_gate_loud=" << loud_peak
                  << " soft_gate=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 6;
    }

    if (probe_micro_tune) {
        constexpr float probe_pi = 3.14159265358979323846f;
        std::vector<ksound::micro_tune::EqBandSpec> bands = {
            {160.0f, 3.5f, 0.8f},
            {2500.0f, -4.0f, 1.3f},
        };
        ksound::micro_tune::Settings settings;
        settings.processing_enabled = true;
        settings.input_gain_db = 6.0f;
        settings.noise_suppression_enabled = false;
        settings.eq_bands = bands;
        ksound::micro_tune::Processor dsp;
        std::vector<float> frames(CHUNK_SAMPLES, 0.0f);
        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            const float t = static_cast<float>(frame) / RATE;
            const float sample =
                (0.25f * std::sin(2.0f * probe_pi * 220.0f * t))
                + (0.12f * std::sin(2.0f * probe_pi * 2200.0f * t));
            frames[frame * CHANNELS] = sample;
            frames[frame * CHANNELS + 1] = sample * 0.85f;
        }
        dsp.process_pre_gate(frames, settings);
        dsp.process_post_gate(frames, settings);
        float sum_abs = 0.0f;
        float weighted = 0.0f;
        float peak = 0.0f;
        for (std::size_t i = 0; i < frames.size(); ++i) {
            const float sample = frames[i];
            sum_abs += std::fabs(sample);
            weighted += sample * static_cast<float>((i % 17) + 1);
            peak = std::max(peak, std::fabs(sample));
        }
        const bool ok = std::isfinite(sum_abs) && std::isfinite(weighted) && std::isfinite(peak);
        std::cout
            << "micro_tune_sum=" << sum_abs << " "
            << "micro_tune_weighted=" << weighted << " "
            << "micro_tune_peak=" << peak << " "
            << "micro_tune=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 6;
    }

    if (probe_fun_voice_fx) {
        constexpr float probe_pi = 3.14159265358979323846f;
        std::vector<float> dry(CHUNK_SAMPLES, 0.0f);
        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            const float t = static_cast<float>(frame) / RATE;
            const float sample = 0.34f * std::sin(2.0f * probe_pi * 220.0f * t)
                + 0.17f * std::sin(2.0f * probe_pi * 440.0f * t);
            for (int ch = 0; ch < CHANNELS; ++ch) dry[frame * CHANNELS + ch] = sample;
        }
        bool ok = true;
        for (const auto& effect : {std::string("distortion"), std::string("pitch_down"), std::string("pitch_up"), std::string("reverb")}) {
            ksound::voice_fx::Processor dsp;
            std::vector<float> frames = dry;
            double diff = 0.0;
            for (int i = 0; i < 8; ++i) {
                frames = dry;
                dsp.process(frames, {effect}, 100.0f);
                for (std::size_t n = 0; n < frames.size(); ++n) diff += std::fabs(frames[n] - dry[n]);
            }
            std::cout << effect << "_diff=" << diff << " ";
            ok = ok && std::isfinite(diff) && diff > 5.0;
        }
        std::cout << "fun_voice_fx=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 7;
    }

    if (probe_custom_fx) {
        constexpr float probe_pi = 3.14159265358979323846f;
        MicroState probe_state;
        probe_state.custom_fx_enabled = true;
        probe_state.custom_fx_wet = 100.0f;
        probe_state.custom_fx_modules = {
            {"highpass", 180.0f, 0.0f, 0.0f},
            {"lowpass", 5200.0f, 0.0f, 0.0f},
            {"saturation", 2.4f, 0.0f, 0.0f},
            {"delay", 45.0f, 18.0f, 24.0f},
        };
        ksound::voice_fx::Processor dsp;
        float difference = 0.0f;
        float peak = 0.0f;
        bool finite = true;
        for (int chunk = 0; chunk < 20; ++chunk) {
            std::vector<float> frames(CHUNK_SAMPLES, 0.0f);
            for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
                const float t = static_cast<float>((chunk * CHUNK_FRAMES) + frame) / RATE;
                const float sample =
                    (0.30f * std::sin(2.0f * probe_pi * 110.0f * t))
                    + (0.18f * std::sin(2.0f * probe_pi * 900.0f * t))
                    + (0.08f * std::sin(2.0f * probe_pi * 7200.0f * t));
                for (int ch = 0; ch < CHANNELS; ++ch) frames[frame * CHANNELS + ch] = sample;
            }
            const auto dry = frames;
            dsp.process_custom(frames, probe_state.custom_fx_enabled, probe_state.custom_fx_wet, probe_state.custom_fx_modules);
            for (std::size_t i = 0; i < frames.size(); ++i) {
                finite = finite && std::isfinite(frames[i]);
                difference += std::fabs(frames[i] - dry[i]);
                peak = std::max(peak, std::fabs(frames[i]));
            }
        }
        const bool ok = finite && difference > 1.0f && peak > 0.01f && peak <= 1.21f;
        std::cout << "custom_fx_diff=" << difference << " custom_fx_peak=" << peak
                  << " custom_fx=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 6;
    }

    if (probe_voice_fx) {
        constexpr float probe_pi = 3.14159265358979323846f;
        const std::array<std::string, 5> presets = {"radio", "telephone", "church", "church_pa", "robot"};
        bool ok = true;
        for (const auto& preset : presets) {
            MicroState probe_state;
            probe_state.voice_fx_enabled = true;
            probe_state.voice_fx_preset = preset;
            probe_state.voice_fx_active = {preset};
            probe_state.voice_fx_amount = 100.0f;
            ksound::voice_fx::Processor dsp;
            float difference = 0.0f;
            float energy = 0.0f;
            for (int chunk = 0; chunk < 10; ++chunk) {
                std::vector<float> frames(CHUNK_SAMPLES, 0.0f);
                for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
                    const float t = static_cast<float>((chunk * CHUNK_FRAMES) + frame) / RATE;
                    const float sample =
                        (0.28f * std::sin(2.0f * probe_pi * 180.0f * t))
                        + (0.18f * std::sin(2.0f * probe_pi * 720.0f * t))
                        + (0.10f * std::sin(2.0f * probe_pi * 3200.0f * t));
                    for (int ch = 0; ch < CHANNELS; ++ch) {
                        frames[frame * CHANNELS + ch] = sample;
                    }
                }
                const auto dry = frames;
                dsp.process(frames, probe_state.voice_fx_active, probe_state.voice_fx_amount);
                for (std::size_t i = 0; i < frames.size(); ++i) {
                    if (!std::isfinite(frames[i])) ok = false;
                    difference += std::fabs(frames[i] - dry[i]);
                    energy += std::fabs(frames[i]);
                }
            }
            const bool changed = difference > 0.5f && energy > 0.1f;
            ok = ok && changed;
            std::cout << preset << "_diff=" << difference << " ";
        }
        // Synthetic plosive guard: a short 80 Hz burst approximates the low
        // frequency energy from a close-mic P/B. Measure only the delayed tail,
        // where the Church de-plosive filter should prevent a large reverb puff.
        for (const auto& preset : {std::string("church"), std::string("church_pa")}) {
            MicroState probe_state;
            probe_state.voice_fx_enabled = true;
            probe_state.voice_fx_preset = preset;
            probe_state.voice_fx_active = {preset};
            probe_state.voice_fx_amount = 100.0f;
            ksound::voice_fx::Processor dsp;
            float tail_energy = 0.0f;
            float tail_peak = 0.0f;
            for (int chunk = 0; chunk < 40; ++chunk) {
                std::vector<float> frames(CHUNK_SAMPLES, 0.0f);
                for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
                    const int absolute_frame = chunk * CHUNK_FRAMES + frame;
                    const float t = static_cast<float>(absolute_frame) / RATE;
                    float sample = 0.0f;
                    if (absolute_frame < 480) {
                        const float envelope = 1.0f - (static_cast<float>(absolute_frame) / 480.0f);
                        sample = 0.92f * envelope * std::sin(2.0f * probe_pi * 80.0f * t);
                    }
                    for (int ch = 0; ch < CHANNELS; ++ch) frames[frame * CHANNELS + ch] = sample;
                }
                dsp.process(frames, probe_state.voice_fx_active, probe_state.voice_fx_amount);
                if (chunk >= 6) {
                    for (float sample : frames) {
                        const float magnitude = std::fabs(sample);
                        tail_energy += magnitude;
                        tail_peak = std::max(tail_peak, magnitude);
                    }
                }
            }
            if (!std::isfinite(tail_energy) || !std::isfinite(tail_peak) || tail_peak > 0.80f) ok = false;
            std::cout << preset << "_plosive_tail=" << tail_energy << " "
                      << preset << "_plosive_peak=" << tail_peak << " ";
        }
        std::cout << "voice_fx=" << (ok ? "ok" : "failed") << "\n";
        return ok ? 0 : 5;
    }

    if (probe_processing_telemetry) {
        constexpr float probe_pi = 3.14159265358979323846f;

        ksound::micro_tune::Settings compressor_settings;
        compressor_settings.processing_enabled = true;
        compressor_settings.compressor_enabled = true;
        compressor_settings.compressor_amount = 100.0f;
        ksound::micro_tune::Processor compressor_dsp;
        std::vector<float> compressor_frames(CHUNK_SAMPLES, 0.80f);
        for (int i = 0; i < 6; ++i) {
            compressor_dsp.process_post_gate(compressor_frames, compressor_settings);
        }
        const float compressor_db = compressor_dsp.telemetry().compressor_reduction_db;

        ksound::micro_tune::Settings deesser_settings;
        deesser_settings.processing_enabled = true;
        deesser_settings.deesser_enabled = true;
        deesser_settings.deesser_amount = 100.0f;

        ksound::micro_tune::Processor deesser_vowel_dsp;
        std::vector<float> deesser_vowel_frames(CHUNK_SAMPLES, 0.0f);
        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            const float t = static_cast<float>(frame) / RATE;
            const float sample =
                (0.42f * std::sin(2.0f * probe_pi * 200.0f * t))
                + (0.20f * std::sin(2.0f * probe_pi * 400.0f * t))
                + (0.12f * std::sin(2.0f * probe_pi * 800.0f * t))
                + (0.08f * std::sin(2.0f * probe_pi * 1200.0f * t))
                + (0.05f * std::sin(2.0f * probe_pi * 2000.0f * t))
                + (0.03f * std::sin(2.0f * probe_pi * 3000.0f * t));
            for (int ch = 0; ch < CHANNELS; ++ch) {
                deesser_vowel_frames[frame * CHANNELS + ch] = sample;
            }
        }
        for (int i = 0; i < 6; ++i) {
            deesser_vowel_dsp.process_post_gate(deesser_vowel_frames, deesser_settings);
        }
        const float deesser_vowel_db = deesser_vowel_dsp.telemetry().deesser_reduction_db;

        ksound::micro_tune::Processor deesser_sibilant_dsp;
        std::vector<float> deesser_sibilant_frames(CHUNK_SAMPLES, 0.0f);
        for (int frame = 0; frame < CHUNK_FRAMES; ++frame) {
            const float t = static_cast<float>(frame) / RATE;
            const float sample =
                (0.52f * std::sin(2.0f * probe_pi * 6500.0f * t))
                + (0.22f * std::sin(2.0f * probe_pi * 8300.0f * t));
            for (int ch = 0; ch < CHANNELS; ++ch) {
                deesser_sibilant_frames[frame * CHANNELS + ch] = sample;
            }
        }
        for (int i = 0; i < 6; ++i) {
            deesser_sibilant_dsp.process_post_gate(deesser_sibilant_frames, deesser_settings);
        }
        const float deesser_sibilant_db = deesser_sibilant_dsp.telemetry().deesser_reduction_db;

        ksound::micro_tune::Settings limiter_settings;
        limiter_settings.processing_enabled = true;
        limiter_settings.limiter_enabled = true;
        limiter_settings.limiter_ceiling_db = -12.0f;
        ksound::micro_tune::Processor limiter_dsp;
        std::vector<float> limiter_frames(CHUNK_SAMPLES, 0.95f);
        limiter_dsp.process_final_mix(limiter_frames, limiter_settings);
        const float limiter_db = limiter_dsp.telemetry().limiter_reduction_db;

        std::cout
            << "compressor_db=" << compressor_db << " "
            << "deesser_vowel_db=" << deesser_vowel_db << " "
            << "deesser_sibilant_db=" << deesser_sibilant_db << " "
            << "limiter_db=" << limiter_db << "\n";
        const bool deesser_selective =
            deesser_vowel_db < 1.0f
            && deesser_sibilant_db > 2.0f
            && deesser_sibilant_db > (deesser_vowel_db + 2.0f);
        const bool ok = compressor_db > 0.05f
            && deesser_selective
            && limiter_db > 0.05f;
        return ok ? 0 : 4;
    }

    if (state_path.empty()) {
        std::cerr << "usage: ksound_native_micro_engine --state <path> [--levels <path>] [--log <path>] [--period-ms 10]\n";
        return 2;
    }

    std::ofstream log;
    if (!log_path.empty()) {
        log.open(log_path, std::ios::app);
        if (log) {
            log << "native micro engine start v2 micro_bus+retour\n";
        }
    }

    PlaybackClient micro_playback;
    micro_playback.set_sink("micro_bus");

    PlaybackClient return_playback;
    return_playback.set_sink("retour");

    std::map<std::string, CaptureClient> captures;
    std::string last_signature;
    StateFileCache state_cache;
    // Keep one reusable buffer for the exact stream sent to both sinks.
    std::vector<float> prepared_micro_mix;
    prepared_micro_mix.reserve(CHUNK_SAMPLES);
    ksound::micro_tune::Processor micro_tune;
    ksound::hard_gate::Processor micro_gate;
    ksound::voice_fx::Processor micro_voice_fx;
    ksound::voice_fx::Processor micro_custom_fx;
    std::string micro_gate_source;
    float micro_gain_current = 1.0f;

    const auto period = std::chrono::milliseconds(period_ms);
    auto next = std::chrono::steady_clock::now();
    auto report_at = next + std::chrono::seconds(30);
    std::uint64_t cycles = 0;
    std::uint64_t late_cycles = 0;
    double max_cycle_ms = 0.0;
    double max_monitor_write_ms = 0.0;
    double max_capture_wait_ms = 0.0;
    double max_mix_and_dsp_ms = 0.0;
    double max_micro_write_ms = 0.0;
    double max_meter_export_ms = 0.0;
    double max_scheduler_wakeup_ms = 0.0;

    while (g_running) {
        const auto cycle_start = std::chrono::steady_clock::now();
        // Positive oversleep only. Never adjust the established 10ms schedule.
        max_scheduler_wakeup_ms = std::max(max_scheduler_wakeup_ms,
            std::max(0.0, std::chrono::duration<double, std::milli>(
                cycle_start - next).count()));
        state_cache.refresh(state_path);
        const MicroState& state = state_cache.state;
        const std::string& signature = state_cache.signature;

        std::set<std::string> wanted_sources;

        if (state.enabled && !state.muted && !state.mic_source.empty()) {
            wanted_sources.insert(state.mic_source);
        }

        if (state.enabled && !state.muted) {
            for (const auto& send : state.sends) {
                wanted_sources.insert(send.source_name);
            }
        }

        if (state.return_enabled && !state.return_muted) {
            for (const auto& source : state.return_sources) {
                wanted_sources.insert(source.source_name);
            }
        }

        if (signature != last_signature) {
            for (auto it = captures.begin(); it != captures.end();) {
                if (!wanted_sources.count(it->first)) {
                    it->second.stop();
                    it = captures.erase(it);
                } else {
                    ++it;
                }
            }

            return_playback.set_sink(state.return_target_sink.empty() ? "retour" : state.return_target_sink);

            last_signature = signature;
            if (log) {
                log << "state reload:"
                    << " mic_enabled=" << state.enabled
                    << " mic_muted=" << state.muted
                    << " mic_source=" << state.mic_source
                    << " hard_gate=" << state.hard_gate_enabled
                    << " hard_gate_threshold=" << state.hard_gate_threshold
                    << " processing=" << state.processing_enabled
                    << " input_gain_db=" << state.input_gain_db
                    << " rnnoise=" << state.noise_suppression
                    << " gate=" << state.gate_enabled
                    << " gate_threshold=" << state.gate_threshold
                    << " eq_bands=" << state.eq_bands.size()
                    << " compressor=" << state.compressor_enabled
                    << " compressor_amount=" << state.compressor_amount
                    << " deesser=" << state.deesser_enabled
                    << " deesser_amount=" << state.deesser_amount
                    << " limiter=" << state.limiter_enabled
                    << " limiter_ceiling_db=" << state.limiter_ceiling_db
                    << " voice_fx=" << state.voice_fx_enabled
                    << " voice_fx_preset=" << state.voice_fx_preset
                    << " voice_fx_amount=" << state.voice_fx_amount
                    << " sends=" << state.sends.size()
                    << " native_monitor=" << state.native_monitor_enabled
                    << " return_enabled=" << state.return_enabled
                    << " return_muted=" << state.return_muted
                    << " return_sources=" << state.return_sources.size()
                    << " return_target=" << state.return_target_sink
                    << "\n";
            }
        }

        std::map<std::string, std::vector<float>> chunks;
        std::vector<float> silence(CHUNK_SAMPLES, 0.0f);

        auto chunk_for = [&](const std::string& source_name) -> const std::vector<float>& {
            if (source_name.empty()) {
                return silence;
            }

            auto found = chunks.find(source_name);
            if (found != chunks.end()) {
                return found->second;
            }

            auto& client = captures[source_name];
            client.source_name = source_name;
            auto chunk = client.read_chunk();
            auto inserted = chunks.emplace(source_name, std::move(chunk));
            return inserted.first->second;
        };

        std::vector<float> micro_mix(CHUNK_SAMPLES, 0.0f);
        std::vector<float> return_mix(CHUNK_SAMPLES, 0.0f);
        const auto mix_and_dsp_start = std::chrono::steady_clock::now();

        if (state.enabled && !state.muted) {
            const float mic_gain = volume_percent_to_gain(state.volume_percent);

            if (!state.mic_source.empty()) {
                if (micro_gate_source != state.mic_source) {
                    micro_gate.reset();
                    micro_tune.reset();
                    micro_voice_fx.reset();
                    micro_custom_fx.reset();
                    micro_gate_source = state.mic_source;
                    micro_gain_current = 0.0f;
                }

                const auto capture_start = std::chrono::steady_clock::now();
                std::vector<float> mic_chunk = chunk_for(state.mic_source);
                max_capture_wait_ms = std::max(max_capture_wait_ms,
                    std::chrono::duration<double, std::milli>(
                        std::chrono::steady_clock::now() - capture_start).count());
                const auto tune = micro_tune_settings(state);
                micro_tune.process_pre_gate(mic_chunk, tune);
                // Hard Gate is intentionally independent of Processing. It
                // remains usable as the already-validated fast gate even
                // when every new DSP stage is bypassed.
                micro_gate.process(mic_chunk, hard_gate_settings(state));
                micro_tune.process_post_gate(mic_chunk, tune);
                // Voice FX is intentionally independent from TUNE/Processing
                // and applies only to the physical MICRO input. Soundboard and
                // channel injections are mixed afterwards and remain clean.
                micro_voice_fx.process(mic_chunk, state.voice_fx_active, state.voice_fx_amount);
                // Generic IP14 preset chain: hot-reloaded from the state file,
                // still physical-MICRO-only so Soundboard/injections stay clean.
                micro_custom_fx.process_custom(mic_chunk, state.custom_fx_enabled, state.custom_fx_wet, state.custom_fx_modules);
                mix_add_ramped(micro_mix, mic_chunk, micro_gain_current, mic_gain);
            }

            for (const auto& send : state.sends) {
                mix_add(micro_mix, chunk_for(send.source_name), clampf(send.gain, 0.0f, 2.0f));
            }
        } else {
            micro_gain_current = 0.0f;
        }

        // User limiter is a smooth final stage for the complete virtual mic
        // mix. PlaybackClient still keeps its independent emergency hard
        // ceiling as a last-resort safety net.
        micro_tune.process_final_mix(micro_mix, micro_tune_settings(state));
        // Includes capture wait, mixing and DSP: not a pure CPU-time reading.
        max_mix_and_dsp_ms = std::max(max_mix_and_dsp_ms,
            std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - mix_and_dsp_start).count());
        // One DSP pass, two consumers: the exported virtual MICRO and the
        // optional MIC OUT feed. Prefer the monitor write first, since a full
        // micro_bus pipe must not delay the feedback path unnecessarily.
        // The downstream retour renderer retains its own EQ/gain/mute.
        // Prepare the signal ONCE so MICRO and native MIC OUT are bit-identical
        // and no repeated allocation/peak scan sits in the monitoring path.
        prepared_micro_mix.assign(micro_mix.begin(), micro_mix.end());
        PlaybackClient::sanitize_in_place(prepared_micro_mix);
        const auto monitor_write_start = std::chrono::steady_clock::now();
        if (state.native_monitor_enabled) {
            return_playback.write_prepared(prepared_micro_mix);
        }
        max_monitor_write_ms = std::max(max_monitor_write_ms,
            std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - monitor_write_start).count());
        const auto micro_write_start = std::chrono::steady_clock::now();
        micro_playback.write_prepared(prepared_micro_mix);
        max_micro_write_ms = std::max(max_micro_write_ms,
            std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - micro_write_start).count());

        if (!state.native_monitor_enabled && state.return_enabled && !state.return_muted && !state.return_sources.empty()) {
            const float return_gain = volume_percent_to_gain(state.return_volume_percent);
            for (const auto& source : state.return_sources) {
                mix_add(return_mix, chunk_for(source.source_name), clampf(source.gain, 0.0f, 2.0f) * return_gain);
            }
            return_playback.write(return_mix);
        } else if (!state.native_monitor_enabled) {
            return_playback.stop();
        }

        const auto meter_export_start = std::chrono::steady_clock::now();
        write_levels_file(
            levels_path,
            micro_mix,
            state.native_monitor_enabled ? micro_mix : return_mix,
            micro_tune.telemetry()
        );
        max_meter_export_ms = std::max(max_meter_export_ms,
            std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - meter_export_start).count());

        ++cycles;
        const auto cycle_end = std::chrono::steady_clock::now();
        max_cycle_ms = std::max(max_cycle_ms,
            std::chrono::duration<double, std::milli>(cycle_end - cycle_start).count());
        next += period;
        const auto now = cycle_end;
        if (now > next + std::chrono::milliseconds(1)) {
            ++late_cycles;
        }
        if (now >= report_at) {
            // One line per 30s: observable stability without per-tick I/O.
            // These are engine timing/backpressure indicators, NOT measured
            // microphone-to-headset latency or PipeWire xrun counters.
            std::uint64_t capture_short = 0;
            std::uint64_t capture_trim = 0;
            std::size_t max_capture_queued_after_read_frames = 0;
            for (auto& [_, client] : captures) {
                capture_short += client.short_reads;
                capture_trim += client.stale_trims;
                max_capture_queued_after_read_frames = std::max(
                    max_capture_queued_after_read_frames,
                    client.max_queued_after_read_frames);
                client.max_queued_after_read_frames = 0;
            }
            if (log) {
                log << "stable-monitor 30s cycles=" << cycles
                    << " max_cycle_ms=" << max_cycle_ms
                    << " late_cycles=" << late_cycles
                    << " max_monitor_write_ms=" << max_monitor_write_ms
                    << " max_capture_wait_ms=" << max_capture_wait_ms
                    << " max_mix_and_dsp_ms=" << max_mix_and_dsp_ms
                    << " max_micro_write_ms=" << max_micro_write_ms
                    << " max_meter_export_ms=" << max_meter_export_ms
                    << " max_scheduler_wakeup_ms=" << max_scheduler_wakeup_ms
                    << " max_capture_queued_after_read_ms="
                    << (1000.0 * max_capture_queued_after_read_frames / RATE)
                    << " state_reads=" << state_cache.reads
                    << " state_cache_hits=" << state_cache.hits
                    << " capture_short_total=" << capture_short
                    << " capture_trim_total=" << capture_trim
                    << " monitor_backpressure_total=" << return_playback.backpressure_drops
                    << " micro_backpressure_total=" << micro_playback.backpressure_drops
                    << std::endl;
            }
            cycles = 0;
            late_cycles = 0;
            max_cycle_ms = 0.0;
            max_monitor_write_ms = 0.0;
            max_capture_wait_ms = 0.0;
            max_mix_and_dsp_ms = 0.0;
            max_micro_write_ms = 0.0;
            max_meter_export_ms = 0.0;
            max_scheduler_wakeup_ms = 0.0;
            report_at = now + std::chrono::seconds(30);
        }
        if (now > next + period * 4) {
            // A delayed scheduler must only reset its deadline. Killing every
            // live parec/pacat here makes the recovery itself take longer than
            // the 40 ms threshold; with multiple sources that can create an
            // endless resync -> stop -> respawn loop and a permanently silent
            // micro_bus. CaptureClient already bounds/drains stale audio and
            // each client restarts itself if its child really exits.
            next = std::chrono::steady_clock::now() + period;
            if (log) {
                log << "scheduler deadline reset after pause\n";
            }
        }
        std::this_thread::sleep_until(next);
    }

    for (auto& [_, client] : captures) {
        client.stop();
    }
    micro_playback.stop();
    return_playback.stop();

    if (log) {
        log << "native micro engine stop\n";
    }

    return 0;
}
