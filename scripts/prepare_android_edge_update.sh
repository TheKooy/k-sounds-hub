#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO"
APK="dist/KSoundsSoundboardRemote.apk"
ASSET="dist/K-Sounds-Soundboard-Remote-26092203.apk"
CERT="265171e886d952245c5994ed035aa049f184bb6cee12e87c240789f133a91e38"
CODE="26092203"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/android-sdk}}"
TOOLS="${ANDROID_BUILD_TOOLS_DIR:-$SDK/build-tools/37.0.0}"
manifest="android-soundboard-apk/AndroidManifest.xml"
require_build_baseline(){
 [[ -f "$manifest" ]] || { echo "Missing manifest" >&2; exit 3; }
 grep -qF 'android:versionCode="26092203"' "$manifest" || { echo "Unexpected Android versionCode" >&2; exit 3; }
 grep -qF 'channel.setShowBadge(false);' android-soundboard-apk/src/com/kooy/ksoundsoundboard/FloatingFavoritesService.java || { echo "Badge fix missing" >&2; exit 3; }
}
verify_apk(){
 local file="$1" output digest badging
 [[ -s "$file" ]] || { echo "No APK: $file" >&2; exit 4; }
 output="$("$TOOLS/apksigner" verify --verbose --print-certs "$file")"
 grep -q 'Verifies' <<<"$output" || { echo "Signature verification failed" >&2; exit 4; }
 digest="$(sed -n 's/.*Signer: certificate SHA-256 digest: //p' <<<"$output" | head -1 | tr -d ':' | tr '[:upper:]' '[:lower:]')"
 [[ "$digest" == "$CERT" ]] || { echo "APK signing certificate mismatch: $digest" >&2; exit 4; }
 badging="$("$TOOLS/aapt" dump badging "$file")"
 grep -q "package: name='com.kooy.ksoundsoundboard' versionCode='$CODE'" <<<"$badging" || { echo "Package/version mismatch" >&2; exit 4; }
 echo "APK checked: $file (versionCode $CODE, historical signing key verified)"
}
case "${1:-}" in
 build)
  require_build_baseline
  KEYSTORE="$HOME/.android/debug.keystore"
  [[ -f "$KEYSTORE" ]] || { echo "Historic Android debug.keystore missing. STOP." >&2; exit 3; }
  digest="$(keytool -list -v -keystore "$KEYSTORE" -alias androiddebugkey -storepass android | sed -n 's/.*SHA256: //p' | head -1 | tr -d ':' | tr '[:upper:]' '[:lower:]')"
  [[ "$digest" == "$CERT" ]] || { echo "Historic signing keystore mismatch. STOP." >&2; exit 3; }
  grep -q -- '--release 8' scripts/build_android_soundboard_apk.sh || { echo "The Java 8 javac correction is missing in your build script. STOP." >&2; exit 3; }
  SIGN_TMP="$(mktemp -d)"
  trap 'rm -rf -- "$SIGN_TMP"' EXIT
  KSOUND_SOUNDBOARD_SIGN_DIR="$SIGN_TMP" \
  KSOUND_SOUNDBOARD_KEYSTORE="$KEYSTORE" \
  KSOUND_SOUNDBOARD_KEYSTORE_PASS=android \
  KSOUND_SOUNDBOARD_KEY_PASS=android \
  KSOUND_SOUNDBOARD_KEY_ALIAS=androiddebugkey \
  bash scripts/build_android_soundboard_apk.sh
  verify_apk "$APK"
  cp -f -- "$APK" "$ASSET"
  sha256sum "$ASSET"
  echo "Prepared GitHub release asset: $REPO/$ASSET"
  echo "Not published: use '$0 publish TheKooy/k-sounds-hub' explicitly after reviewing the build."
  ;;
 publish)
  repo="${2:-}"
  [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "Usage: $0 publish owner/public-repo" >&2; exit 3; }
  require_build_baseline
  command -v gh >/dev/null || { echo "GitHub CLI missing: install gh, then gh auth login" >&2; exit 3; }
  verify_apk "$ASSET"
  visibility="$(gh repo view "$repo" --json visibility --jq '.visibility')"
  [[ "$visibility" == "PUBLIC" ]] || { echo "Updater only sees public GitHub releases. Repo $repo is $visibility" >&2; exit 3; }
  tag="remote-android-26092203"
  if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
    echo "Release $tag already exists: refusing to overwrite it." >&2; exit 3
  fi
  echo "Publicly publishing: $repo, $tag, $(basename "$ASSET")"
  read -r -p "Type PUBLISH to upload this APK as a public stable release: " confirm
  [[ "$confirm" == PUBLISH ]] || { echo "Cancelled"; exit 2; }
  gh release create "$tag" "$ASSET#K-Sounds-Soundboard-Remote-26092203.apk" \
    --repo "$repo" --title 'K-Sounds Remote 0.4.9-edge-dev2 (IP15e1)' \
    --notes 'Edge favorites Android APK. Requires K-Sounds Remote IP15e web server. Removes launcher badge for floating service; foreground service notification remains. Signed with historical K-Sounds Android certificate.' \
    --latest
  echo "Published. In Remote > App updates & display, set repository to $repo, then Check for updates."
  ;;
 *) echo "Usage: $0 build | publish owner/public-repo" >&2; exit 2;;
esac
