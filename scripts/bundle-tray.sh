#!/usr/bin/env bash
# Wrap the SwiftPM StructorTray executable in a .app bundle and (optionally)
# install it into /Applications.
#
#   scripts/bundle-tray.sh            build tray/.build/StructorTray.app
#   scripts/bundle-tray.sh install    ... then copy to /Applications and relaunch
#
# The bundle is LSUIElement (menu bar only, no Dock icon) and ad-hoc signed so
# Gatekeeper on this Mac accepts a locally built binary.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TRAY="$APP_DIR/tray"
BUNDLE="$TRAY/.build/StructorTray.app"
DEST="${DEST:-/Applications/StructorTray.app}"
VERSION="$(git -C "$APP_DIR" describe --tags --always --dirty 2>/dev/null || echo dev)"

(cd "$TRAY" && swift build -c release 2>&1 | tail -1)

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$TRAY/.build/release/StructorTray" "$BUNDLE/Contents/MacOS/StructorTray"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>      <string>en</string>
  <key>CFBundleExecutable</key>             <string>StructorTray</string>
  <key>CFBundleIdentifier</key>             <string>studio.soulbrews.structor.tray</string>
  <key>CFBundleInfoDictionaryVersion</key>  <string>6.0</string>
  <key>CFBundleName</key>                   <string>StructorTray</string>
  <key>CFBundleDisplayName</key>            <string>Structor Tray</string>
  <key>CFBundlePackageType</key>            <string>APPL</string>
  <key>CFBundleShortVersionString</key>     <string>${VERSION}</string>
  <key>CFBundleVersion</key>                <string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key>         <string>13.0</string>
  <key>LSUIElement</key>                    <true/>
  <key>NSHighResolutionCapable</key>        <true/>
  <key>NSHumanReadableCopyright</key>       <string>Soul Brews Studio</string>
</dict>
</plist>
PLIST

echo 'APPL????' > "$BUNDLE/Contents/PkgInfo"
codesign --force --sign - "$BUNDLE" >/dev/null 2>&1 || echo "warn: ad-hoc codesign failed (continuing)"
echo "bundle: $BUNDLE ($VERSION)"

if [ "${1:-}" = "install" ]; then
  # stop any running copy (repo build or installed) before swapping the bundle
  pkill -x StructorTray 2>/dev/null || true
  sleep 1
  rm -rf "$DEST"
  ditto "$BUNDLE" "$DEST"
  open -a "$DEST"
  sleep 2
  if pgrep -x StructorTray >/dev/null; then
    echo "installed and running: $DEST"
  else
    echo "installed but not running: $DEST — try: open -a '$DEST'"; exit 1
  fi
fi
