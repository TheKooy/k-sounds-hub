#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(python - <<'PY'
from pathlib import Path
import re
text = Path('src/ksound_hub/_version.py').read_text(encoding='utf-8')
match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
if not match:
    raise SystemExit('Could not read version from src/ksound_hub/_version.py')
print(match.group(1))
PY
)"
fi

TAG="v$VERSION"
DIST_DIR="$REPO_DIR/dist"
BUILD_ROOT="$(mktemp -d /tmp/k-sounds-release-build.XXXXXX)"
PACKAGE_DIR="$BUILD_ROOT/k-sounds-hub-linux-release-$TAG"
TAR_PATH="$DIST_DIR/k-sounds-hub-linux-release-$TAG.tar.gz"
ZIP_PATH="$DIST_DIR/k-sounds-hub-linux-release-$TAG.zip"
SUMS_PATH="$DIST_DIR/SHA256SUMS.txt"

cleanup_build_root() {
  if [[ "$BUILD_ROOT" == /tmp/k-sounds-release-build.* ]]; then
    rm -rf -- "$BUILD_ROOT"
  fi
}
trap cleanup_build_root EXIT

mkdir -p "$PACKAGE_DIR" "$DIST_DIR"

GIT_TOP="$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "$GIT_TOP" == "$REPO_DIR" ]]; then
  # Build release contents from committed Git state only.
  # This prevents local files such as .continueignore, temporary context docs,
  # ignored build directories, backups, APKs, keystores, and dirty worktree files
  # from leaking into public release archives.
  git archive --format=tar HEAD | tar -xf - -C "$PACKAGE_DIR"
elif command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='build/' \
    --exclude='dist/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='.mypy_cache/' \
    --exclude='.rsync-tmp/' \
    --exclude='.continueignore' \
    --exclude='CONTINUE_KSOUNDS_CONTEXT.md' \
    --exclude='.ksh_*/' \
    --exclude='.soundboard_*/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.jks' \
    --exclude='*.keystore' \
    --exclude='*.apk' \
    --exclude='*.tar.gz' \
    --exclude='*.zip' \
    "$REPO_DIR/" "$PACKAGE_DIR/"
else
  (cd "$REPO_DIR" && tar \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./venv' \
    --exclude='./build' \
    --exclude='*/build' \
    --exclude='./dist' \
    --exclude='*/.rsync-tmp' \
    --exclude='./.continueignore' \
    --exclude='./CONTINUE_KSOUNDS_CONTEXT.md' \
    --exclude='./.ksh_*' \
    --exclude='./.soundboard_*' \
    -cf - .) | tar -xf - -C "$PACKAGE_DIR"
fi

if [[ -n "${KSH_APK_PATH:-}" ]]; then
  if [[ ! -f "$KSH_APK_PATH" ]]; then
    echo "KSH_APK_PATH does not exist: $KSH_APK_PATH" >&2
    exit 1
  fi
  mkdir -p "$PACKAGE_DIR/optional/android"
  cp -a "$KSH_APK_PATH" "$PACKAGE_DIR/optional/android/KSoundsSoundboardRemote.apk"
fi

RELEASE_EPOCH="${SOURCE_DATE_EPOCH:-}"
if [[ -z "$RELEASE_EPOCH" && "$GIT_TOP" == "$REPO_DIR" ]]; then
  RELEASE_EPOCH="$(git -C "$REPO_DIR" show -s --format=%ct HEAD)"
fi
if [[ -z "$RELEASE_EPOCH" ]]; then
  RELEASE_EPOCH="$(date +%s)"
fi

# Normalize copied metadata so an old asset timestamp cannot make extraction
# noisy and SOURCE_DATE_EPOCH can produce deterministic release timestamps.
find "$PACKAGE_DIR" -exec touch -h -d "@$RELEASE_EPOCH" {} +

chmod +x \
  "$PACKAGE_DIR/install.sh" \
  "$PACKAGE_DIR/uninstall.sh" \
  "$PACKAGE_DIR/scripts/"*.sh \
  "$PACKAGE_DIR/tests/fixtures/fake_installer_python.sh"

bash -n "$PACKAGE_DIR/install.sh"
bash -n "$PACKAGE_DIR/uninstall.sh"
for f in "$PACKAGE_DIR/scripts/"*.sh; do bash -n "$f"; done
python3 "$PACKAGE_DIR/scripts/check_launch_contract.py"
python3 "$PACKAGE_DIR/scripts/check_install_contract.py"
python3 "$PACKAGE_DIR/scripts/check_runtime_lifecycle_contract.py"
python3 "$PACKAGE_DIR/scripts/check_version_consistency.py"

rm -f "$TAR_PATH" "$ZIP_PATH" "$SUMS_PATH"
(
  cd "$BUILD_ROOT"
  tar \
    --sort=name \
    --mtime="@$RELEASE_EPOCH" \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    -czf "$TAR_PATH" \
    "$(basename "$PACKAGE_DIR")"
)
python3 - "$PACKAGE_DIR" "$ZIP_PATH" "$RELEASE_EPOCH" <<'PY'
from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
import stat
import sys
import time


package_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])
epoch = max(int(sys.argv[3]), 315532800)  # ZIP timestamps start in 1980.
date_time = time.gmtime(epoch)[:6]
base_dir = package_dir.parent
paths = [package_dir, *package_dir.rglob("*")]
paths.sort(key=lambda path: path.relative_to(base_dir).as_posix())

with ZipFile(zip_path, "w") as archive:
    for path in paths:
        relative = path.relative_to(base_dir).as_posix()
        is_directory = path.is_dir()
        archive_name = f"{relative}/" if is_directory else relative
        info = ZipInfo(archive_name, date_time=date_time)
        info.create_system = 3
        mode = stat.S_IMODE(path.stat().st_mode)
        info.external_attr = (mode & 0xFFFF) << 16
        if is_directory:
            info.external_attr |= 0x10
            archive.writestr(info, b"", compress_type=ZIP_STORED)
        else:
            archive.writestr(
                info,
                path.read_bytes(),
                compress_type=ZIP_DEFLATED,
                compresslevel=9,
            )

with ZipFile(zip_path) as archive:
    broken = archive.testzip()
    if broken is not None:
        raise SystemExit(f"ZIP integrity error: {broken}")
PY

"$PACKAGE_DIR/scripts/check_release_archive.sh" "$TAR_PATH"

(
  cd "$DIST_DIR"
  sha256sum "$(basename "$TAR_PATH")" "$(basename "$ZIP_PATH")" > "$SUMS_PATH"
)

cat <<EOF_DONE
Release files created:
  $TAR_PATH
  $ZIP_PATH
  $SUMS_PATH

Optional APK included: $([[ -n "${KSH_APK_PATH:-}" ]] && echo yes || echo no)
EOF_DONE
