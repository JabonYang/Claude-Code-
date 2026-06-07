#!/bin/bash
set -e

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

PLIST_NAME="com.ych.feishu-bot"
PLIST_SRC="$PROJECT_DIR/scripts/$PLIST_NAME.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

# Detect local bin paths
LOCAL_BIN="$HOME/.local/bin"
HOMEBREW_BIN=""
if [ -d "/opt/homebrew/bin" ]; then
    HOMEBREW_BIN="/opt/homebrew/bin"
elif [ -d "/usr/local/Homebrew/bin" ]; then
    HOMEBREW_BIN="/usr/local/Homebrew/bin"
fi

echo "=== Installing Feishu Bot as launchd service ==="
echo "Project: $PROJECT_DIR"
echo "Venv:    $VENV_DIR"

# Unload existing if present
if launchctl list "$PLIST_NAME" &>/dev/null; then
    echo "Stopping existing service..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# Generate plist from template
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__VENV_DIR__|$VENV_DIR|g" \
    -e "s|__LOCAL_BIN__|$LOCAL_BIN|g" \
    -e "s|__HOMEBREW_BIN__|$HOMEBREW_BIN|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Load service
launchctl load "$PLIST_DST"

echo ""
echo "Service installed and started!"
echo "  Status:  launchctl list | grep feishu"
echo "  Logs:    tail -f /tmp/feishu-server.log"
echo "  Stop:    launchctl unload $PLIST_DST"
echo "  Start:   launchctl load $PLIST_DST"
