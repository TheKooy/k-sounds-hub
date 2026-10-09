#!/usr/bin/env bash
# Build with the existing app signing key so Android can update without losing pairing.
set -Eeuo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
sdk_root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
if [[ -z "$sdk_root" || ! -d "$sdk_root/platforms" ]]; then
    echo 'Set ANDROID_SDK_ROOT to an Android SDK with a platform and build-tools installed.' >&2
    exit 2
fi
signing_key="$HOME/.android/debug.keystore"
expected_certificate='265171e886d952245c5994ed035aa049f184bb6cee12e87c240789f133a91e38'
if [[ ! -f "$signing_key" ]]; then
    echo 'The verified June APK key is required at ~/.android/debug.keystore. No key will be generated.' >&2
    exit 2
fi
for command in javac zip; do
    command -v "$command" >/dev/null || { echo "Missing build tool: $command" >&2; exit 2; }
done
build_tools="$(find "$sdk_root/build-tools" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -n 1)"
android_jar="$(find "$sdk_root/platforms" -name android.jar | sort -V | tail -n 1)"
for binary in aapt2 d8 zipalign apksigner; do
    [[ -x "$build_tools/$binary" ]] || { echo "Missing Android build tool: $binary" >&2; exit 2; }
done
[[ -f "$build_tools/core-lambda-stubs.jar" ]] || { echo 'Missing Android lambda stubs; install current build-tools.' >&2; exit 2; }
[[ -f "$android_jar" ]] || { echo 'No Android platform found.' >&2; exit 2; }
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/ksh-remote-apk.XXXXXX")"
trap 'rm -r -- "$build_dir"' EXIT
mkdir -p "$build_dir/classes" "$build_dir/dex"
app_dir="$repo_root/android-soundboard-apk"
"$build_tools/aapt2" compile --dir "$app_dir/res" -o "$build_dir/resources.zip"
"$build_tools/aapt2" link -I "$android_jar" --manifest "$app_dir/AndroidManifest.xml" \
    --min-sdk-version 23 --target-sdk-version 28 --auto-add-overlay \
    -o "$build_dir/unsigned.apk" "$build_dir/resources.zip"
mapfile -t sources < <(find "$app_dir/src" -name '*.java' -type f | sort)
javac -encoding UTF-8 -source 8 -target 8 -bootclasspath "$android_jar:$build_tools/core-lambda-stubs.jar" \
    -d "$build_dir/classes" "${sources[@]}"
mapfile -t classes < <(find "$build_dir/classes" -name '*.class' -type f | sort)
"$build_tools/d8" --lib "$android_jar" --min-api 23 --output "$build_dir/dex" "${classes[@]}"
zip -jq "$build_dir/unsigned.apk" "$build_dir"/dex/*.dex
"$build_tools/zipalign" -f 4 "$build_dir/unsigned.apk" "$build_dir/aligned.apk"
output_dir="${KSH_ANDROID_OUTPUT_DIR:-$repo_root/dist}"
mkdir -p "$output_dir"
version_code="$(sed -n 's/.*android:versionCode="\([0-9]*\)".*/\1/p' "$app_dir/AndroidManifest.xml")"
[[ "$version_code" =~ ^[0-9]+$ ]] || { echo 'Invalid Android versionCode.' >&2; exit 2; }
output="$output_dir/K-Sounds-Soundboard-Remote-$version_code.apk"
KSH_LOCAL_STORE_SECRET=android KSH_LOCAL_KEY_SECRET=android \
"$build_tools/apksigner" sign --ks "$signing_key" --ks-key-alias androiddebugkey \
    --ks-pass env:KSH_LOCAL_STORE_SECRET --key-pass env:KSH_LOCAL_KEY_SECRET \
    --out "$build_dir/signed.apk" "$build_dir/aligned.apk"
"$build_tools/apksigner" verify --print-certs "$build_dir/signed.apk" > "$build_dir/certificates.txt"
mapfile -t certificates < <(
    sed -nE 's/^.*certificate SHA-256 digest:[[:space:]]*([0-9A-Fa-f:]+).*$/\1/p' "$build_dir/certificates.txt" |
        tr -d ':' |
        tr '[:upper:]' '[:lower:]' |
        sort -u
)
if (( ${#certificates[@]} != 1 )); then
    echo 'Could not determine a unique signing certificate SHA-256 digest.' >&2
    exit 2
fi
certificate="${certificates[0]}"
if [[ "$certificate" != "$expected_certificate" ]]; then
    echo 'Certificate mismatch: this is not the verified June APK key. Existing output was preserved.' >&2
    exit 2
fi
cp -- "$build_dir/signed.apk" "$output"
echo "Built and verified against the June APK certificate: $output"
