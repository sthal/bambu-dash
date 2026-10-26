#!/bin/sh
# Builds "Mission Control.app" (menu bar app with the server, Python and ffmpeg inside) and macos/build/MissionControl.dmg.
# Needs Xcode or the Command Line Tools (swiftc). Downloads are cached in macos/build/cache.
set -e
cd "$(dirname "$0")"
ROOT=..
PY_TAG=20261021
PY_VER=3.12.15
B=build
C=$B/cache
APP="$B/Mission Control.app"
mkdir -p "$C"

fetch() {  # url file
    [ -s "$C/$2" ] && return
    curl -fsSL "$1" -o "$C/$2.part" && mv "$C/$2.part" "$C/$2" || { echo "download failed: $1"; exit 1; }
}
for a in aarch64 x86_64; do
    fetch "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/cpython-$PY_VER+$PY_TAG-$a-apple-darwin-install_only_stripped.tar.gz" "python-$a.tar.gz"
done
for a in arm64 amd64; do
    fetch "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$a/release/ffmpeg.zip" "ffmpeg-$a.zip"
done

echo "== app bundle"
rm -rf "$APP"
R="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$R/app"
for a in aarch64 x86_64; do
    d=$([ $a = aarch64 ] && echo arm64 || echo x86_64)
    mkdir -p "$R/python-$d"
    tar -xzf "$C/python-$a.tar.gz" -C "$R/python-$d" --strip-components 1
    rm -rf "$R/python-$d/lib/python3.12/test" "$R/python-$d/lib/python3.12/idlelib" "$R/python-$d/lib/python3.12/tkinter" \
           "$R/python-$d/lib/python3.12/ensurepip" "$R/python-$d/share" "$R/python-$d/include"
done
unzip -oq "$C/ffmpeg-arm64.zip" -d "$B/ff-arm64" && mv "$B/ff-arm64/ffmpeg" "$R/ffmpeg-arm64" && rm -rf "$B/ff-arm64"
unzip -oq "$C/ffmpeg-amd64.zip" -d "$B/ff-amd64" && mv "$B/ff-amd64/ffmpeg" "$R/ffmpeg-x86_64" && rm -rf "$B/ff-amd64"

# the server itself, without secrets or local state
cp $ROOT/server.py $ROOT/history.py $ROOT/toolpath.py "$R/app/"
cp -R $ROOT/static $ROOT/data "$R/app/"
"$R/python-arm64/bin/python3" -m pip install -q --disable-pip-version-check --no-compile --target "$R/app/vendor" -r $ROOT/requirements.txt

echo "== swift"
for a in arm64 x86_64; do
    swiftc -O -parse-as-library -target $a-apple-macos13.0 MissionControl.swift -o "$B/mc-$a"
done
lipo -create "$B/mc-arm64" "$B/mc-x86_64" -output "$APP/Contents/MacOS/MissionControl"
rm -f "$B/mc-arm64" "$B/mc-x86_64"

echo "== icon"
I=$B/AppIcon.iconset
rm -rf $I && mkdir -p $I
SRC=$ROOT/ios/MissionControl/Assets.xcassets/AppIcon.appiconset/icon-1024.png
for s in 16 32 128 256 512; do
    sips -z $s $s "$SRC" --out $I/icon_${s}x${s}.png >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$SRC" --out $I/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns $I -o "$R/AppIcon.icns" && rm -rf $I

VERSION=$(git -C $ROOT log -1 --format=%cd --date=format:%Y.%m.%d)
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Mission Control</string>
  <key>CFBundleDisplayName</key><string>Mission Control</string>
  <key>CFBundleIdentifier</key><string>com.sthal.missioncontrol.mac</string>
  <key>CFBundleExecutable</key><string>MissionControl</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Mission Control connects to your Bambu Lab printer on the local network.</string>
</dict>
</plist>
PLIST

echo "== sign (ad-hoc)"
find "$R" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -exec codesign --force -s - {} \; 2>/dev/null
codesign --force -s - "$APP"
codesign --verify --deep --strict "$APP"

echo "== dmg"
S=$B/dmg
rm -rf $S "$B/MissionControl.dmg" && mkdir -p $S
cp -R "$APP" $S/ && ln -s /Applications $S/Applications
hdiutil create -quiet -volname "Mission Control" -srcfolder $S -fs HFS+ -format ULMO "$B/MissionControl.dmg"
rm -rf $S
ls -lh "$B/MissionControl.dmg"
