#pragma once

#include <memory>
#include <string>
#include <vector>

namespace ksound::voice_fx {

struct CustomModuleSpec {
    std::string type;
    float p1{0.0f};
    float p2{0.0f};
    float p3{0.0f};
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
    void process(
        std::vector<float>& frames,
        const std::vector<std::string>& active_effects,
        float amount_percent
    );
    void process_custom(
        std::vector<float>& frames,
        bool enabled,
        float wet_percent,
        const std::vector<CustomModuleSpec>& modules
    );

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace ksound::voice_fx
