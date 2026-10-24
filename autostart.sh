#!/bin/sh
# Start Mission Control at login and keep it running (macOS, launchd).
#   ./autostart.sh           install / update
#   ./autostart.sh --remove  uninstall
cd "$(dirname "$0")" || exit 1
LABEL=local.mission-control
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
if [ "$1" = "--remove" ]; then
    rm -f "$PLIST"
    echo "Автозапуск убран / Autostart removed"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$PWD/run.sh</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$PWD/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$PWD/server.log</string>
  <key>StandardErrorPath</key><string>$PWD/server.log</string>
</dict>
</plist>
PLIST

if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
    echo "Не удалось включить автозапуск / Could not enable autostart"
    exit 1
fi
echo "Автозапуск включён: сервер стартует при входе в систему и перезапускается, если упадёт."
echo "Autostart enabled: the server starts at login and restarts if it crashes."
echo "Лог / Log: $PWD/server.log   Убрать / Remove: ./autostart.sh --remove"
