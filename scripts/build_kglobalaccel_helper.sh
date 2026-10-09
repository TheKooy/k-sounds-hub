#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SRC="$ROOT/tools/kglobalaccel_helper"
BUILD="$SRC/build"
OUT="$ROOT/bin/ksound-kglobalaccel-helper"

[[ -f "$SRC/CMakeLists.txt" ]] || {
  echo "Missing KGlobalAccel helper source: $SRC" >&2
  exit 2
}

cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD" --parallel

[[ -x "$BUILD/ksound-kglobalaccel-helper" ]] || {
  echo "KGlobalAccel helper build produced no executable" >&2
  exit 3
}

mkdir -p "$ROOT/bin"
install -m 0755 "$BUILD/ksound-kglobalaccel-helper" "$OUT"
printf 'Installed %s\n' "$OUT"
