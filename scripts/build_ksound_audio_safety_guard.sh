#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:-$HOME/k-sound-hub-v2}"
SRC="$REPO/native_engine/src/finite_guard_ladspa.c"
BUILD_DIR="$REPO/native_engine/build"
OUT="$BUILD_DIR/ksounds_finite_guard.so"

[[ -f "$SRC" ]] || { echo "Finite guard source introuvable: $SRC" >&2; exit 1; }

CC_BIN="${CC:-}"
if [[ -z "$CC_BIN" ]]; then
  if command -v cc >/dev/null 2>&1; then
    CC_BIN="$(command -v cc)"
  elif command -v gcc >/dev/null 2>&1; then
    CC_BIN="$(command -v gcc)"
  else
    echo "Compilateur C introuvable (cc/gcc)" >&2
    exit 1
  fi
fi

mkdir -p "$BUILD_DIR"
"$CC_BIN" \
  -std=c11 -O2 -fPIC -shared \
  -Wall -Wextra -Werror \
  "$SRC" -o "$OUT" -lm

python3 - "$OUT" <<'PY'
import ctypes
import math
import struct
import sys

path = sys.argv[1]

class Hint(ctypes.Structure):
    _fields_ = [
        ("HintDescriptor", ctypes.c_int),
        ("LowerBound", ctypes.c_float),
        ("UpperBound", ctypes.c_float),
    ]

Instantiate = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
Connect = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_float))
Run = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)
Cleanup = ctypes.CFUNCTYPE(None, ctypes.c_void_p)

class Descriptor(ctypes.Structure):
    _fields_ = [
        ("UniqueID", ctypes.c_ulong),
        ("Label", ctypes.c_char_p),
        ("Properties", ctypes.c_int),
        ("Name", ctypes.c_char_p),
        ("Maker", ctypes.c_char_p),
        ("Copyright", ctypes.c_char_p),
        ("PortCount", ctypes.c_ulong),
        ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
        ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
        ("PortRangeHints", ctypes.POINTER(Hint)),
        ("ImplementationData", ctypes.c_void_p),
        ("instantiate", Instantiate),
        ("connect_port", Connect),
        ("activate", ctypes.c_void_p),
        ("run", Run),
        ("run_adding", ctypes.c_void_p),
        ("set_run_adding_gain", ctypes.c_void_p),
        ("deactivate", ctypes.c_void_p),
        ("cleanup", Cleanup),
    ]

lib = ctypes.CDLL(path)
lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
lib.ladspa_descriptor.restype = ctypes.POINTER(Descriptor)
ptr = lib.ladspa_descriptor(0)
if not ptr:
    raise SystemExit("ladspa_descriptor(0) absent")
d = ptr.contents
assert d.UniqueID == 0x4B5346, hex(d.UniqueID)
assert d.Label == b"ksounds_finite_guard_mono", d.Label
assert d.Properties & 0x4, d.Properties
assert d.PortCount == 2, d.PortCount
assert [d.PortDescriptors[i] for i in range(2)] == [0x9, 0xA]

raw_bits = [
    0x00000000,  # +0
    0x80000000,  # -0
    0x3F000000,  # 0.5
    0xBF800000,  # -1.0
    0x3FA00000,  # 1.25 (must NOT be clamped)
    0x7F7FFFFF,  # largest finite float32
    0x00000001,  # positive subnormal
    0x7FC00000,  # qNaN
    0x7F800000,  # +Inf
    0xFF800000,  # -Inf
]
raw = b"".join(struct.pack("<I", bits) for bits in raw_bits)
values = list(struct.unpack("<" + "f" * len(raw_bits), raw))
Input = ctypes.c_float * len(values)
Output = ctypes.c_float * len(values)
inbuf = Input(*values)
outbuf = Output()
handle = d.instantiate(ctypes.cast(ptr, ctypes.c_void_p), 48000)
assert handle
try:
    d.connect_port(handle, 0, inbuf)
    d.connect_port(handle, 1, outbuf)
    d.run(handle, len(values))
finally:
    d.cleanup(handle)

for index in range(7):
    before = struct.pack("<f", values[index])
    after = struct.pack("<f", outbuf[index])
    assert after == before, (index, before.hex(), after.hex())
for index in range(7, 10):
    assert outbuf[index] == 0.0 and math.isfinite(outbuf[index]), (index, outbuf[index])

print(f"Finite Safety Guard OK: {path}")
print("Policy verified: finite float32 bit-identical; NaN/+Inf/-Inf -> 0.0")
PY

printf '%s\n' "$OUT"
