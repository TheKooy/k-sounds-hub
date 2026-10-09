#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:-$HOME/k-sound-hub-v2}"
SRC_DIR="$REPO/native_engine"
BUILD_DIR="$SRC_DIR/build"

[[ -d "$SRC_DIR" ]] || { echo "Native engine source introuvable: $SRC_DIR" >&2; exit 1; }
command -v cmake >/dev/null 2>&1 || { echo "cmake introuvable" >&2; exit 1; }
command -v c++ >/dev/null 2>&1 || command -v g++ >/dev/null 2>&1 || { echo "compilateur C++ introuvable" >&2; exit 1; }

JOBS=2
if command -v nproc >/dev/null 2>&1; then
  JOBS="$(nproc)"
fi

cmake -S "$SRC_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_DIR" \
  --target \
    ksound_native_engine \
    ksound_native_micro_engine \
    ksound_native_meter_engine \
  -j"$JOBS"


"$REPO/scripts/build_ksound_audio_safety_guard.sh" "$REPO"

echo
for binary in \
  ksound_native_engine \
  ksound_native_micro_engine \
  ksound_native_meter_engine
do
  path="$BUILD_DIR/$binary"
  [[ -x "$path" ]] || { echo "Binaire natif manquant après build: $path" >&2; exit 1; }
  ls -l "$path"
done
