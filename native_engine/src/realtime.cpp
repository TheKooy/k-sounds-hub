#include "realtime.hpp"

#include <sched.h>
#include <string>
#include <sys/mman.h>

namespace ksound::native {

std::string try_enable_realtime() {
    std::string result;

    // Lock only the pages already present. MCL_FUTURE can turn a normal
    // capture backlog into allocation failures when RLIMIT_MEMLOCK is low.
    if (mlockall(MCL_CURRENT) == 0) {
        result += "mlockall_current=ok ";
    } else {
        result += "mlockall_current=fail ";
    }

    // Do not put the whole native engine in SCHED_FIFO.
    // It spawns parec/pacat children; if they inherit realtime scheduling,
    // playback can lag badly or destabilize.
    result += "sched_fifo=disabled";

    return result;
}

} // namespace ksound::native
