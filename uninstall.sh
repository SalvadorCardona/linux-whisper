#!/bin/sh
# Complete uninstallation of whisper-desk (Linux, WSL and macOS).
#   curl -LsSf https://raw.githubusercontent.com/SalvadorCardona/whisper-desk/main/uninstall.sh | sh
# Add WD_PURGE=1 to remove the configuration and the history as well.
set -eu

AGENT_LABEL="fr.whisperdesk.daemon"
AGENT="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"

say() { printf '==> %s\n' "$1"; }

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    say "Stopping the service"
    systemctl --user disable --now whisper-desk.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/whisper-desk.service"
    systemctl --user daemon-reload || true
fi

if command -v launchctl >/dev/null 2>&1 && [ -f "$AGENT" ]; then
    say "Stopping the launchd agent"
    launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 \
        || launchctl unload -w "$AGENT" >/dev/null 2>&1 || true
    rm -f "$AGENT"
fi

if [ -x "$HOME/.local/bin/whisper-desk" ]; then
    say "Stopping the daemon and removing the keyboard shortcut"
    "$HOME/.local/bin/whisper-desk" quit >/dev/null 2>&1 || true
    "$HOME/.local/bin/whisper-desk" hotkey remove >/dev/null 2>&1 || true
    pkill -f "whisper_desk tray" >/dev/null 2>&1 || true
fi

say "Removing the menu entries and the tray icon"
for entry in settings history; do
    rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/whisper-desk-$entry.desktop"
done
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/whisper-desk-tray.desktop"

say "Removing the files"
rm -f "$HOME/.local/bin/whisper-desk"
rm -rf "$HOME/.local/share/whisper-desk"

if [ "${WD_PURGE:-0}" = "1" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/whisper-desk"
    rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/whisper-desk"
    say "Configuration and history removed"
else
    say "Configuration kept (WD_PURGE=1 to remove it)"
fi

printf '\nwhisper-desk is uninstalled.\n'
printf 'The Whisper models stay in ~/.cache/huggingface (rm -rf to get rid of them).\n'
