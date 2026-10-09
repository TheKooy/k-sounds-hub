#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cerrno>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <map>
#include <poll.h>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

constexpr std::size_t CHANNELS = 2;
constexpr std::size_t SAMPLE_BYTES = sizeof(float);
constexpr std::size_t FRAME_BYTES = CHANNELS * SAMPLE_BYTES;
constexpr std::size_t RATE = 48000;
// A meter must describe what is happening now. Retaining a short window is
// enough to catch transients without ever replaying seconds of stale samples.
constexpr std::size_t RECENT_WINDOW_BYTES = RATE * FRAME_BYTES * 30 / 1000; // 30 ms
// A permanently hot/misbehaving producer must not monopolize the meter loop.
// Hitting this limit means freshness can no longer be guaranteed, so the
// capture is restarted and its upstream Pulse/PipeWire backlog is discarded.
constexpr std::size_t MAX_DRAIN_BYTES = RATE * FRAME_BYTES / 4; // 250 ms
constexpr int DRAIN_SETTLE_MS = 1;

static std::atomic<bool> g_stop{false};

static void on_signal(int) {
    g_stop.store(true);
}

struct SourceSpec {
    std::string key;
    std::string source;
};

struct Capture {
    SourceSpec spec;
    pid_t pid = -1;
    int fd = -1;
    std::vector<char> buffer;
    float left = 0.0f;
    float right = 0.0f;
    std::chrono::steady_clock::time_point last_start{};
};

static void append_recent(Capture& cap, const char* data, std::size_t size) {
    cap.buffer.insert(cap.buffer.end(), data, data + size);
    if (cap.buffer.size() <= RECENT_WINDOW_BYTES) {
        return;
    }

    std::size_t drop = cap.buffer.size() - RECENT_WINDOW_BYTES;
    const std::size_t remainder = drop % FRAME_BYTES;
    if (remainder != 0) {
        drop += FRAME_BYTES - remainder;
    }
    cap.buffer.erase(
        cap.buffer.begin(),
        cap.buffer.begin() + static_cast<std::ptrdiff_t>(drop)
    );
}

static std::string now_string() {
    auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    return std::to_string(static_cast<long long>(now));
}

static void stop_capture(Capture& cap) {
    if (cap.fd >= 0) {
        close(cap.fd);
        cap.fd = -1;
    }

    if (cap.pid > 0) {
        kill(cap.pid, SIGTERM);
        for (int i = 0; i < 10; ++i) {
            int status = 0;
            pid_t got = waitpid(cap.pid, &status, WNOHANG);
            if (got == cap.pid || got == -1) {
                cap.pid = -1;
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        kill(cap.pid, SIGKILL);
        waitpid(cap.pid, nullptr, 0);
        cap.pid = -1;
    }
}

static bool start_capture(Capture& cap) {
    auto now = std::chrono::steady_clock::now();
    if (cap.last_start.time_since_epoch().count() != 0) {
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - cap.last_start).count();
        if (elapsed < 1000) {
            return false;
        }
    }
    cap.last_start = now;

    int pipefd[2];
    if (pipe(pipefd) != 0) {
        return false;
    }

    pid_t pid = fork();
    if (pid == 0) {
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        close(pipefd[1]);

        std::string device = "--device=" + cap.spec.source;
        execlp(
            "parec",
            "parec",
            device.c_str(),
            "--raw",
            "--format=float32le",
            "--rate=48000",
            "--channels=2",
            "--latency-msec=25",
            static_cast<char*>(nullptr)
        );
        _exit(127);
    }

    close(pipefd[1]);

    if (pid < 0) {
        close(pipefd[0]);
        return false;
    }

    int flags = fcntl(pipefd[0], F_GETFL, 0);
    if (flags >= 0) {
        fcntl(pipefd[0], F_SETFL, flags | O_NONBLOCK);
    }

    cap.pid = pid;
    cap.fd = pipefd[0];
    cap.buffer.clear();
    cap.left = 0.0f;
    cap.right = 0.0f;
    return true;
}

static void process_buffer(Capture& cap) {
    size_t frames = cap.buffer.size() / FRAME_BYTES;
    if (frames == 0) {
        return;
    }

    const float* samples = reinterpret_cast<const float*>(cap.buffer.data());
    float peak_l = 0.0f;
    float peak_r = 0.0f;

    for (size_t i = 0; i < frames; ++i) {
        float l = samples[i * 2 + 0];
        float r = samples[i * 2 + 1];
        if (std::isfinite(l)) peak_l = std::max(peak_l, std::fabs(l));
        if (std::isfinite(r)) peak_r = std::max(peak_r, std::fabs(r));
    }

    // Publish the newest measured peak only. UI ballistics own attack/release,
    // so smoothing here would merely add delay and make the meter feel heavy.
    cap.left = std::min(1.0f, peak_l);
    cap.right = std::min(1.0f, peak_r);

    size_t used = frames * FRAME_BYTES;
    if (used >= cap.buffer.size()) {
        cap.buffer.clear();
    } else {
        cap.buffer.erase(cap.buffer.begin(), cap.buffer.begin() + static_cast<long>(used));
    }
}

static void read_capture(Capture& cap) {
    if (cap.pid <= 0 || cap.fd < 0) {
        start_capture(cap);
        cap.left = 0.0f;
        cap.right = 0.0f;
        return;
    }

    bool got_data = false;
    bool drain_limit_hit = false;
    std::size_t drained = 0;
    auto settle_deadline = std::chrono::steady_clock::time_point{};
    char tmp[65536];

    for (;;) {
        ssize_t n = read(cap.fd, tmp, sizeof(tmp));
        if (n > 0) {
            got_data = true;
            const auto size = static_cast<std::size_t>(n);
            drained += size;
            if (settle_deadline.time_since_epoch().count() == 0) {
                settle_deadline = std::chrono::steady_clock::now()
                    + std::chrono::milliseconds(DRAIN_SETTLE_MS);
            }
            append_recent(cap, tmp, size);
            if (drained >= MAX_DRAIN_BYTES) {
                drain_limit_hit = true;
                break;
            }
            continue;
        }

        if (n == 0) {
            stop_capture(cap);
            cap.left = 0.0f;
            cap.right = 0.0f;
            return;
        }

        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            // A blocked parec writer may need a scheduler turn after we empty
            // the kernel pipe. Give it a very small grace window so we drain
            // its already-existing upstream backlog, without waiting for the
            // next real-time audio period.
            if (got_data && std::chrono::steady_clock::now() < settle_deadline) {
                pollfd pfd{};
                pfd.fd = cap.fd;
                pfd.events = POLLIN;
                const int ready = ::poll(&pfd, 1, 1);
                if (ready > 0 || (ready < 0 && errno == EINTR)) {
                    continue;
                }
                continue;
            }
            break;
        }

        stop_capture(cap);
        cap.left = 0.0f;
        cap.right = 0.0f;
        return;
    }

    if (drain_limit_hit) {
        // Closing and recreating parec is the only reliable way to discard an
        // additional backlog held upstream by pipewire-pulse itself.
        stop_capture(cap);
        cap.buffer.clear();
        cap.left = 0.0f;
        cap.right = 0.0f;
        return;
    }

    if (got_data) {
        process_buffer(cap);
    } else {
        cap.left = 0.0f;
        cap.right = 0.0f;
    }

    int status = 0;
    pid_t got = waitpid(cap.pid, &status, WNOHANG);
    if (got == cap.pid) {
        cap.pid = -1;
        if (cap.fd >= 0) {
            close(cap.fd);
            cap.fd = -1;
        }
    }
}

static std::string json_escape(const std::string& input) {
    std::ostringstream out;
    for (char c : input) {
        switch (c) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default: out << c; break;
        }
    }
    return out.str();
}

static void write_levels(const std::string& path, const std::vector<Capture>& captures) {
    std::string tmp = path + ".tmp";

    std::ofstream f(tmp, std::ios::out | std::ios::trunc);
    if (!f.good()) {
        return;
    }

    f << "{\n";
    f << "  \"timestamp\": " << now_string() << ",\n";
    f << "  \"channels\": {\n";

    for (size_t i = 0; i < captures.size(); ++i) {
        const auto& cap = captures[i];
        f << "    \"" << json_escape(cap.spec.key) << "\": ["
          << std::max(0.0f, std::min(1.0f, cap.left)) << ", "
          << std::max(0.0f, std::min(1.0f, cap.right)) << "]";
        if (i + 1 < captures.size()) {
            f << ",";
        }
        f << "\n";
    }

    f << "  }\n";
    f << "}\n";
    f.close();

    rename(tmp.c_str(), path.c_str());
}

int main(int argc, char** argv) {
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    std::string levels_path;
    std::string log_path;
    int period_ms = 80;
    std::vector<SourceSpec> sources;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--levels" && i + 1 < argc) {
            levels_path = argv[++i];
        } else if (arg == "--log" && i + 1 < argc) {
            log_path = argv[++i];
        } else if (arg == "--period-ms" && i + 1 < argc) {
            period_ms = std::max(20, std::min(500, std::atoi(argv[++i])));
        } else if (arg == "--source" && i + 1 < argc) {
            std::string spec = argv[++i];
            auto pos = spec.find('=');
            if (pos != std::string::npos && pos > 0 && pos + 1 < spec.size()) {
                sources.push_back({spec.substr(0, pos), spec.substr(pos + 1)});
            }
        }
    }

    if (levels_path.empty() || sources.empty()) {
        std::cerr << "usage: ksound_native_meter_engine --levels <path> [--log <path>] [--period-ms n] --source key=source\n";
        return 2;
    }

    std::ofstream log;
    if (!log_path.empty()) {
        log.open(log_path, std::ios::out | std::ios::app);
        if (log.good()) {
            log << "[" << now_string() << "] native_meter_start period_ms=" << period_ms
                << " sources=" << sources.size() << "\n";
        }
    }

    std::vector<Capture> captures;
    captures.reserve(sources.size());
    for (const auto& spec : sources) {
        Capture cap;
        cap.spec = spec;
        captures.push_back(std::move(cap));
    }

    const auto period = std::chrono::milliseconds(period_ms);
    auto next = std::chrono::steady_clock::now();

    while (!g_stop.load()) {
        for (auto& cap : captures) {
            read_capture(cap);
        }

        write_levels(levels_path, captures);
        next += period;
        const auto now = std::chrono::steady_clock::now();
        if (now > next + period * 4) {
            // Never try to replay missed UI periods after a system pause.
            next = now + period;
        }
        std::this_thread::sleep_until(next);
    }

    for (auto& cap : captures) {
        stop_capture(cap);
    }

    write_levels(levels_path, captures);

    if (log.good()) {
        log << "[" << now_string() << "] native_meter_stop\n";
    }

    return 0;
}
