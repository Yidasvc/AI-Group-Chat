#!/bin/sh
set -eu

APP_DIR="${HOME}/Library/Application Support/CodexGroupChat"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.openai.codex-group-chat.plist"
CLIENT_PATH="${HOME}/.local/bin/cgchat"
SERVICE_LABEL="com.openai.codex-group-chat"
GUI_DOMAIN="gui/$(id -u)"

launchctl bootout "${GUI_DOMAIN}/${SERVICE_LABEL}" 2>/dev/null || true
rm -f "${PLIST_PATH}" "${CLIENT_PATH}"

printf '%s\n' "Service and client launcher removed."
printf '%s\n' "Persistent chat data was intentionally kept at: ${APP_DIR}"
