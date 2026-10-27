#!/usr/bin/env bash
# Run the screener in the background on macOS, starting automatically at login
# and restarting if it ever stops (a launchd agent).
#
#   deploy/macos-autostart.sh install     set up and start now
#   deploy/macos-autostart.sh uninstall   stop and remove
#   deploy/macos-autostart.sh status      is it running?
#   deploy/macos-autostart.sh restart     after pulling new code
#
# Logs: logs/server.log in this folder.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
LABEL="com.screener.sp500"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  install)
    # make sure .venv exists before launchd starts it
    ./run.sh --setup
    mkdir -p logs "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/run.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key><string>${PORT:-8000}</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/logs/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/server.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Installed. The screener runs at http://localhost:${PORT:-8000} and starts at every login."
    echo "Logs: $DIR/logs/server.log"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. Your data in $DIR/data is untouched."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL" && echo "Restarted."
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|pid|last exit code)' || true
    else
      echo "Not installed."
    fi
    ;;
  *)
    sed -n '2,11p' "$0"; exit 1 ;;
esac
