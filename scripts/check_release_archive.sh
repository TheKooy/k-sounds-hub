#!/usr/bin/env bash
set -Eeuo pipefail

ARCHIVE="${1:-}"
[[ -n "$ARCHIVE" ]] || {
  echo "Usage: $0 path/to/k-sounds-hub-linux-release-*.tar.gz" >&2
  exit 2
}
[[ -f "$ARCHIVE" ]] || {
  echo "Release archive not found: $ARCHIVE" >&2
  exit 1
}

ARCHIVE="$(realpath "$ARCHIVE")"
TEST_ROOT="$(mktemp -d /tmp/k-sounds-release-check.XXXXXX)"
LISTING="$TEST_ROOT/archive-files.txt"

cleanup() {
  if [[ "$TEST_ROOT" == /tmp/k-sounds-release-check.* ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}
trap cleanup EXIT

fail() {
  echo "release archive check failed: $*" >&2
  exit 1
}

gzip -t "$ARCHIVE"
tar -tzf "$ARCHIVE" > "$LISTING"

mapfile -t archive_roots < <(awk -F/ 'NF {print $1}' "$LISTING" | sort -u)
[[ "${#archive_roots[@]}" == 1 ]] \
  || fail "archive must contain exactly one top-level directory"

archive_root="${archive_roots[0]}"
[[ "$archive_root" == k-sounds-hub-linux-release-v* ]] \
  || fail "unexpected archive root: $archive_root"

if grep -Eq '(^|/)(\.git|\.venv|venv|__pycache__|build|dist|\.rsync-tmp)/|(^|/)\.ksh_|(^|/)\.soundboard_|\.(pyc|pyo|jks|keystore)$' "$LISTING"; then
  grep -E '(^|/)(\.git|\.venv|venv|__pycache__|build|dist|\.rsync-tmp)/|(^|/)\.ksh_|(^|/)\.soundboard_|\.(pyc|pyo|jks|keystore)$' "$LISTING" >&2
  fail "archive contains excluded development, backup, build or secret files"
fi

tar --no-same-owner -xzf "$ARCHIVE" -C "$TEST_ROOT"
PACKAGE_DIR="$TEST_ROOT/$archive_root"

for required in \
  install.sh \
  uninstall.sh \
  pyproject.toml \
  scripts/start_ksound_hub_v2.sh \
  scripts/check_launch_contract.py \
  scripts/check_install_contract.py \
  scripts/check_runtime_lifecycle_contract.py \
  scripts/test_install_lifecycle.sh \
  tests/fixtures/fake_installer_python.sh \
  src/ksound_hub/glass_app.py
do
  [[ -f "$PACKAGE_DIR/$required" ]] || fail "missing release file: $required"
done

bash -n "$PACKAGE_DIR/install.sh"
bash -n "$PACKAGE_DIR/uninstall.sh"
while IFS= read -r script; do
  bash -n "$script"
done < <(find "$PACKAGE_DIR/scripts" -maxdepth 1 -type f -name '*.sh' | sort)

python3 "$PACKAGE_DIR/scripts/check_launch_contract.py"
python3 "$PACKAGE_DIR/scripts/check_install_contract.py"
python3 "$PACKAGE_DIR/scripts/check_runtime_lifecycle_contract.py"
python3 "$PACKAGE_DIR/scripts/check_version_consistency.py"
"$PACKAGE_DIR/scripts/test_install_lifecycle.sh"

echo "release archive: integrity, contents and isolated install lifecycle passed"
