#!/bin/sh
# Build Mission Control for iPhone and install it with a free Apple ID (Xcode personal team).
#   ./build.sh          generate the Xcode project and open it
#   ./build.sh install  build and install on the connected iPhone
cd "$(dirname "$0")" || exit 1
if ! xcodebuild -version >/dev/null 2>&1; then
    echo "Нужен Xcode из App Store (и один раз: sudo xcode-select -s /Applications/Xcode.app) / Xcode is required"
    exit 1
fi
command -v xcodegen >/dev/null 2>&1 || brew install xcodegen || exit 1
# Your personal team id, taken from Xcode's signing identities (Apple Development: …)
if [ -z "$DEVELOPMENT_TEAM" ]; then
    DEVELOPMENT_TEAM=$(security find-certificate -a -c "Apple Development" -p 2>/dev/null \
        | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' | head -1)
fi
export DEVELOPMENT_TEAM
xcodegen generate --quiet || exit 1
if [ "$1" = "install" ]; then
    [ -n "$DEVELOPMENT_TEAM" ] || { echo "Войдите в Xcode → Settings → Accounts под своим Apple ID / Sign in to Xcode first"; exit 1; }
    DEVICE=$(xcrun devicectl list devices 2>/dev/null | awk '/iPhone/ && /connected|available/ {for (i=1;i<=NF;i++) if ($i ~ /^[0-9A-F-]{25,}$/) {print $i; exit}}')
    [ -n "$DEVICE" ] || { echo "iPhone не найден: подключите кабелем и доверьтесь этому Mac / No iPhone found"; exit 1; }
    xcodebuild -project MissionControl.xcodeproj -scheme MissionControl -configuration Release \
        -destination "generic/platform=iOS" -derivedDataPath build -allowProvisioningUpdates -quiet build || exit 1
    xcrun devicectl device install app --device "$DEVICE" build/Build/Products/Release-iphoneos/MissionControl.app
else
    open MissionControl.xcodeproj
fi
