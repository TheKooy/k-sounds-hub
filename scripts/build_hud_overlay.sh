#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:-$HOME/k-sound-hub-v2}"
SRC_DIR="$REPO/tools/hud_overlay"
BUILD_DIR="$SRC_DIR/build"

[[ -d "$SRC_DIR" ]] || { echo "HUD source introuvable: $SRC_DIR" >&2; exit 1; }
command -v cmake >/dev/null 2>&1 || { echo "cmake introuvable" >&2; exit 1; }
command -v c++ >/dev/null 2>&1 || command -v g++ >/dev/null 2>&1 || { echo "compilateur C++ introuvable" >&2; exit 1; }

JOBS=2
if command -v nproc >/dev/null 2>&1; then
  JOBS="$(nproc)"
fi

cmake -S "$SRC_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_DIR" --target audio-hud-overlay -j"$JOBS"

HUD_BIN="$BUILD_DIR/audio-hud-overlay"
[[ -x "$HUD_BIN" ]] || { echo "Binaire HUD manquant après build: $HUD_BIN" >&2; exit 1; }

echo
ls -l "$HUD_BIN"
