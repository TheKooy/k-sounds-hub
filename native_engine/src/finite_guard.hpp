#pragma once

#include <cmath>
#include <vector>

namespace ksound::native {

inline void sanitize_finite_pcm(std::vector<float>& frames) noexcept {
    for (float& sample : frames) {
        if (!std::isfinite(sample)) {
            sample = 0.0f;
        }
    }
}

}  // namespace ksound::native
