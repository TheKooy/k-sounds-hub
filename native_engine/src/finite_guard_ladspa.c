#include <math.h>
#include <stddef.h>
#include <stdlib.h>

/*
 * Minimal LADSPA 1.1 ABI declarations.  The plugin deliberately has no
 * controls: its only policy is IEEE-754 validity.  Finite samples are copied
 * unchanged; NaN and +/-Inf become neutral digital silence (0.0f).
 */
typedef float LADSPA_Data;
typedef void *LADSPA_Handle;
typedef int LADSPA_Properties;
typedef int LADSPA_PortDescriptor;
typedef int LADSPA_PortRangeHintDescriptor;

#define LADSPA_PROPERTY_HARD_RT_CAPABLE 0x4
#define LADSPA_PORT_INPUT 0x1
#define LADSPA_PORT_OUTPUT 0x2
#define LADSPA_PORT_AUDIO 0x8

typedef struct {
    LADSPA_PortRangeHintDescriptor HintDescriptor;
    LADSPA_Data LowerBound;
    LADSPA_Data UpperBound;
} LADSPA_PortRangeHint;

typedef struct _LADSPA_Descriptor {
    unsigned long UniqueID;
    const char *Label;
    LADSPA_Properties Properties;
    const char *Name;
    const char *Maker;
    const char *Copyright;
    unsigned long PortCount;
    const LADSPA_PortDescriptor *PortDescriptors;
    const char * const *PortNames;
    const LADSPA_PortRangeHint *PortRangeHints;
    void *ImplementationData;
    LADSPA_Handle (*instantiate)(const struct _LADSPA_Descriptor *, unsigned long);
    void (*connect_port)(LADSPA_Handle, unsigned long, LADSPA_Data *);
    void (*activate)(LADSPA_Handle);
    void (*run)(LADSPA_Handle, unsigned long);
    void (*run_adding)(LADSPA_Handle, unsigned long);
    void (*set_run_adding_gain)(LADSPA_Handle, LADSPA_Data);
    void (*deactivate)(LADSPA_Handle);
    void (*cleanup)(LADSPA_Handle);
} LADSPA_Descriptor;

enum {
    PORT_INPUT = 0,
    PORT_OUTPUT = 1,
    PORT_COUNT = 2,
};

typedef struct {
    const LADSPA_Data *input;
    LADSPA_Data *output;
} FiniteGuard;

static LADSPA_Handle guard_instantiate(const LADSPA_Descriptor *descriptor, unsigned long sample_rate) {
    (void)descriptor;
    (void)sample_rate;
    return calloc(1, sizeof(FiniteGuard));
}

static void guard_connect_port(LADSPA_Handle instance, unsigned long port, LADSPA_Data *location) {
    FiniteGuard *guard = (FiniteGuard *)instance;
    if (guard == NULL) {
        return;
    }
    if (port == PORT_INPUT) {
        guard->input = location;
    } else if (port == PORT_OUTPUT) {
        guard->output = location;
    }
}

static void guard_run(LADSPA_Handle instance, unsigned long sample_count) {
    FiniteGuard *guard = (FiniteGuard *)instance;
    if (guard == NULL || guard->input == NULL || guard->output == NULL) {
        return;
    }

    for (unsigned long index = 0; index < sample_count; ++index) {
        const LADSPA_Data sample = guard->input[index];
        guard->output[index] = isfinite(sample) ? sample : 0.0f;
    }
}

static void guard_cleanup(LADSPA_Handle instance) {
    free(instance);
}

static const LADSPA_PortDescriptor PORT_DESCRIPTORS[PORT_COUNT] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
};

static const char * const PORT_NAMES[PORT_COUNT] = {
    "Input",
    "Output",
};

static const LADSPA_PortRangeHint PORT_HINTS[PORT_COUNT] = {
    {0, 0.0f, 0.0f},
    {0, 0.0f, 0.0f},
};

static const LADSPA_Descriptor DESCRIPTOR = {
    0x4B5346,
    "ksounds_finite_guard_mono",
    LADSPA_PROPERTY_HARD_RT_CAPABLE,
    "K-Sounds Finite PCM Guard",
    "K-Sounds",
    "MIT",
    PORT_COUNT,
    PORT_DESCRIPTORS,
    PORT_NAMES,
    PORT_HINTS,
    NULL,
    guard_instantiate,
    guard_connect_port,
    NULL,
    guard_run,
    NULL,
    NULL,
    NULL,
    guard_cleanup,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long index) {
    return index == 0 ? &DESCRIPTOR : NULL;
}
