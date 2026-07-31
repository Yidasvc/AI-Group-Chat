#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR="${HOME}/Library/Application Support/CodexGroupChat"
LOG_DIR="${HOME}/Library/Logs/CodexGroupChat"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/com.openai.codex-group-chat.plist"
BIN_DIR="${HOME}/.local/bin"
CLIENT_PATH="${BIN_DIR}/cgchat"
SERVICE_LABEL="com.openai.codex-group-chat"
GUI_DOMAIN="gui/$(id -u)"
PYTHON_BIN=$(command -v python3)

mkdir -p "${APP_DIR}" "${APP_DIR}/data" "${APP_DIR}/web" "${LOG_DIR}" "${LAUNCH_AGENTS_DIR}" "${BIN_DIR}"
chmod 700 "${APP_DIR}" "${APP_DIR}/data"
install -m 700 "${SOURCE_DIR}/server.py" "${APP_DIR}/server.py"
install -m 700 "${SOURCE_DIR}/cgchat.py" "${APP_DIR}/cgchat.py"
install -m 600 "${SOURCE_DIR}/web/index.html" "${APP_DIR}/web/index.html"
install -m 600 "${SOURCE_DIR}/web/styles.css" "${APP_DIR}/web/styles.css"
install -m 600 "${SOURCE_DIR}/web/app.js" "${APP_DIR}/web/app.js"
ln -sfn "${APP_DIR}/cgchat.py" "${CLIENT_PATH}"

sed \
    -e "s|__PYTHON__|${PYTHON_BIN}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    "${SOURCE_DIR}/com.openai.codex-group-chat.plist.template" > "${PLIST_PATH}.new"
chmod 600 "${PLIST_PATH}.new"
mv "${PLIST_PATH}.new" "${PLIST_PATH}"

launchctl bootout "${GUI_DOMAIN}/${SERVICE_LABEL}" 2>/dev/null || true
sleep 1
bootstrap_ok=0
for attempt in 1 2 3 4 5
do
    if launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_PATH}" 2>/dev/null
    then
        bootstrap_ok=1
        break
    fi
    sleep 1
done
if [ "${bootstrap_ok}" -ne 1 ]
then
    printf '%s\n' "Failed to load ${SERVICE_LABEL}." >&2
    exit 1
fi
launchctl enable "${GUI_DOMAIN}/${SERVICE_LABEL}"
launchctl kickstart -k "${GUI_DOMAIN}/${SERVICE_LABEL}"
launchctl print "${GUI_DOMAIN}/${SERVICE_LABEL}" >/dev/null

printf '%s\n' "Installed Codex Group Chat."
printf '%s\n' "Service: ${SERVICE_LABEL}"
printf '%s\n' "Client:  ${CLIENT_PATH}"
printf '%s\n' "Data:    ${APP_DIR}/data"
printf '%s\n' "Logs:    ${LOG_DIR}"
