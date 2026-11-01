#!/usr/bin/env bash
# Builds a signed release APK at out/ergo.apk.
#
# Set your signing key first, in ~/.gradle/gradle.properties or as environment variables:
#   ERGO_STORE_FILE, ERGO_STORE_PASSWORD, ERGO_KEY_ALIAS, ERGO_KEY_PASSWORD
# Optional: DAEMON_URL bakes in a laptop address (the app asks for one otherwise),
# UPDATE_URL turns on the self-updater (https://<laptop>/app/version.json).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
: "${ANDROID_HOME:?set ANDROID_HOME to your Android SDK}"

args=()
[ -n "${DAEMON_URL:-}" ] && args+=("-PdaemonUrl=$DAEMON_URL")
[ -n "${UPDATE_URL:-}" ] && args+=("-PupdateUrl=$UPDATE_URL")

(cd "$here" && npx cap sync android)
(cd "$here/android" && ./gradlew --quiet assembleRelease "${args[@]}")

apk="$here/out/ergo.apk"
[ -f "$apk" ] || { echo "no signed APK: set the ERGO_* signing values" >&2; exit 1; }
aapt2="$(ls -d "$ANDROID_HOME"/build-tools/*/ | sort -V | tail -1)aapt2"
line="$("$aapt2" dump badging "$apk" | awk 'NR == 1')"
code="$(sed -E "s/.*versionCode='([0-9]+)'.*/\1/" <<<"$line")"
name="$(sed -E "s/.*versionName='([^']*)'.*/\1/" <<<"$line")"
sha="$(sha256sum "$apk" | cut -d' ' -f1)"

# For the self-updater: serve both files from the daemon at /app/.
printf '{"versionCode": %s, "versionName": "%s", "sha256": "%s"}\n' "$code" "$name" "$sha" > "$here/out/version.json"
echo "built $apk, versionCode $code ($name), sha256 $sha"
