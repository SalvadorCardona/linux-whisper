#!/bin/sh
# whisper-desk installation (Linux, WSL and macOS).
#
#   curl -LsSf https://raw.githubusercontent.com/SalvadorCardona/whisper-desk/main/install.sh | sh
#
# Environment variables accepted:
#   WD_REPO   source repository       (default: SalvadorCardona/whisper-desk)
#   WD_REF    branch or tag           (default: main)
#   WD_SRC    local directory to copy (install from a clone, without network)
#   WD_NO_SERVICE=1   do not install the service (systemd or launchd)
#   WD_NO_HOTKEY=1    do not install the keyboard shortcut
#   WD_NO_WELCOME=1   do not open the welcome window after a first installation
set -eu

WD_REPO="${WD_REPO:-SalvadorCardona/whisper-desk}"
WD_REF="${WD_REF:-main}"

APP_DIR="$HOME/.local/share/whisper-desk/app"
# What "whisper-desk update" compares: the copy in APP_DIR has no .git, so
# without this file nothing says which code runs here.
VERSION_FILE="$APP_DIR/.whisper-desk-version"
VENV_DIR="$HOME/.local/share/whisper-desk/venv"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/whisper-desk"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/whisper-desk"
CONFIG="$CONFIG_DIR/config.toml"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/whisper-desk"
UNIT_DIR="$HOME/.config/systemd/user"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
AGENT_DIR="$HOME/Library/LaunchAgents"
AGENT_LABEL="fr.whisperdesk.daemon"

BOLD=""; DIM=""; GREEN=""; YELLOW=""; RESET=""
if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m'); GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m'); RESET=$(printf '\033[0m')
fi

say()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$1"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '%serror:%s %s\n' "$BOLD" "$RESET" "$1" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- 0. host ----------------------------------------------------------------
# The program runs on three hosts: Linux, WSL (Linux inside Windows) and
# macOS. They share everything but their system tools.
HOST=linux
case "$(uname -s)" in
    Darwin) HOST=macos ;;
    Linux)
        if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
            HOST=wsl
        fi
        ;;
    *) die "unsupported system: $(uname -s)" ;;
esac
case "$HOST" in
    linux) HOST_LABEL="Linux" ;;
    wsl)   HOST_LABEL="WSL (Windows)" ;;
    macos) HOST_LABEL="macOS" ;;
esac
say "Detected host: $HOST_LABEL"

# --- 1. system dependencies -------------------------------------------------
say "Checking the system dependencies"
MISSING=""
if [ "$HOST" = macos ]; then
    # sox provides "rec": the microphone capture. The rest (pbcopy, osascript)
    # ships with the system.
    have rec || have sox || have ffmpeg || MISSING="$MISSING sox"
    have pbcopy || warn "pbcopy not found — clipboard unavailable"
    if [ -n "$MISSING" ]; then
        if have brew; then
            warn "missing packages:$MISSING"
            INSTALLED=0
            if (exec 3</dev/tty) 2>/dev/null; then
                printf '    Install them with brew? [Y/n] '
                read -r answer </dev/tty || answer="n"
                case "$answer" in
                    ""|y|Y|yes|Yes|YES)
                        # shellcheck disable=SC2086
                        brew install $MISSING && INSTALLED=1 ;;
                esac
            fi
            [ "$INSTALLED" = 1 ] || warn "to install:${BOLD} brew install$MISSING${RESET}"
        else
            warn "Homebrew missing — install sox by hand:$MISSING"
        fi
    else
        ok "microphone, clipboard and notifications available"
    fi
else
    if [ "$HOST" = wsl ]; then
        # WSLg only carries audio through PulseAudio.
        have parec || have arecord || MISSING="$MISSING pulseaudio-utils"
    else
        have arecord || MISSING="$MISSING alsa-utils"
    fi
    have notify-send || MISSING="$MISSING libnotify-bin"
    if [ "$HOST" = wsl ]; then
        have clip.exe || warn "clip.exe not found — Windows interop seems disabled"
    elif ! have wl-copy && ! have xclip && ! have xsel; then
        MISSING="$MISSING wl-clipboard"
    fi
    # The overlay runs in GTK3 with the system Python.
    if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
        MISSING="$MISSING python3-gi gir1.2-gtk-3.0"
    fi
    # The tray icon is a bonus: its absence is only mentioned.
    if [ "$HOST" = linux ] && ! python3 -c "
import gi
for name in ('AyatanaAppIndicator3', 'AppIndicator3'):
    try:
        gi.require_version(name, '0.1'); raise SystemExit(0)
    except ValueError:
        pass
raise SystemExit(1)" 2>/dev/null; then
        warn "optional, for the tray icon: gir1.2-ayatanaappindicator3-0.1"
    fi

    if [ -n "$MISSING" ]; then
        if have apt-get; then
            warn "missing packages:$MISSING"
            INSTALLED=0
            if (exec 3</dev/tty) 2>/dev/null; then
                printf '    Install them with sudo apt? [Y/n] '
                read -r answer </dev/tty || answer="n"
                case "$answer" in
                    ""|y|Y|yes|Yes|YES)
                        # shellcheck disable=SC2086
                        sudo apt-get update && sudo apt-get install -y $MISSING && INSTALLED=1
                        ;;
                esac
            fi
            [ "$INSTALLED" = 1 ] || warn "to install:${BOLD} sudo apt install$MISSING${RESET}"
        else
            warn "missing packages (install the equivalents):$MISSING"
        fi
    else
        ok "microphone, clipboard, notifications and GTK3 available"
    fi
fi

if [ "$HOST" = linux ] && [ ! -w /dev/uinput ]; then
    warn "/dev/uinput is not writable: insertion at the cursor will stay unavailable"
fi

# --- 2. fetching the sources ------------------------------------------------
mkdir -p "$APP_DIR" "$BIN_DIR" "$CONFIG_DIR"
WD_COMMIT=""
WD_DATE=""
WD_ORIGIN=github
if [ -n "${WD_SRC:-}" ]; then
    say "Copying the sources from $WD_SRC"
    [ -d "$WD_SRC/src/whisper_desk" ] || die "$WD_SRC does not contain src/whisper_desk"
    WD_ORIGIN=local
    # An absolute path: "whisper-desk update" will replay the installation from it.
    WD_SRC=$(cd "$WD_SRC" && pwd)
    if have git && git -C "$WD_SRC" rev-parse --git-dir >/dev/null 2>&1; then
        WD_COMMIT=$(git -C "$WD_SRC" rev-parse HEAD 2>/dev/null || echo "")
        WD_DATE=$(git -C "$WD_SRC" log -1 --format=%cI 2>/dev/null || echo "")
        BRANCH=$(git -C "$WD_SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
        [ "$BRANCH" = HEAD ] || [ -z "$BRANCH" ] || WD_REF="$BRANCH"
    fi
    rm -rf "$APP_DIR"
    mkdir -p "$APP_DIR"
    tar -C "$WD_SRC" --exclude='.git' --exclude='__pycache__' -cf - . | tar -C "$APP_DIR" -xf -
else
    say "Downloading $WD_REPO ($WD_REF)"
    have curl || die "curl is required"
    have tar  || die "tar is required"
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT INT TERM
    # The tarball says nothing about the commit it was cut from: the API does.
    if curl -fsSL -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$WD_REPO/commits/$WD_REF" -o "$TMP/commit.json" 2>/dev/null; then
        # One field per line first: a greedy regex on compact JSON would
        # otherwise catch the last "sha" of the payload instead of the commit's.
        tr ',{' '\n\n' < "$TMP/commit.json" > "$TMP/commit.fields"
        WD_COMMIT=$(sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{7,40\}\)".*/\1/p' \
            "$TMP/commit.fields" | head -1)
        WD_DATE=$(sed -n 's/.*"date"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$TMP/commit.fields" | head -1)
    fi
    curl -fsSL "https://codeload.github.com/$WD_REPO/tar.gz/refs/heads/$WD_REF" -o "$TMP/src.tar.gz" \
        || die "download failed (private repository or nonexistent branch?)"
    rm -rf "$APP_DIR"
    mkdir -p "$APP_DIR"
    tar -xzf "$TMP/src.tar.gz" -C "$APP_DIR" --strip-components=1
fi

# The fingerprint is written last: APP_DIR was wiped just above.
{
    printf 'commit=%s\n' "${WD_COMMIT:-unknown}"
    printf 'ref=%s\n' "$WD_REF"
    printf 'date=%s\n' "${WD_DATE:-unknown}"
    printf 'repo=%s\n' "$WD_REPO"
    printf 'origin=%s\n' "$WD_ORIGIN"
    if [ "$WD_ORIGIN" = local ]; then
        printf 'src=%s\n' "$WD_SRC"
    fi
    printf 'installed=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "$VERSION_FILE"
if [ -n "$WD_COMMIT" ]; then
    ok "sources in $APP_DIR ($WD_REF @ $(printf '%.7s' "$WD_COMMIT"))"
else
    ok "sources in $APP_DIR ($WD_REF, commit unknown)"
fi

# --- 3. isolated Python environment -----------------------------------------
if ! have uv; then
    say "Installing uv (Python environment manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "cannot install uv"
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || die "uv not found after installation"
ok "uv: $UV"

if [ -x "$VENV_DIR/bin/python" ]; then
    say "Updating the existing Python environment"
else
    say "Creating the Python environment (3.12)"
fi
"$UV" venv --python 3.12 --allow-existing "$VENV_DIR" >/dev/null \
    || die "cannot create the Python environment"
say "Installing faster-whisper"
VIRTUAL_ENV="$VENV_DIR"; export VIRTUAL_ENV
"$UV" pip install --quiet faster-whisper || die "cannot install faster-whisper"
ok "faster-whisper installed"

if [ "$HOST" = macos ]; then
    warn "macOS: transcription on the CPU (CTranslate2 does not use Metal)"
elif have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
    say "NVIDIA GPU detected — adding the CUDA libraries"
    "$UV" pip install --quiet "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10" \
        && ok "cuBLAS + cuDNN installed" \
        || warn "CUDA not installed — transcription will run on the CPU"
else
    warn "no NVIDIA GPU — transcription on the CPU ('small' model by default)"
fi

# --- 4. executable ----------------------------------------------------------
SYSTEM_PYTHON="$(command -v python3 || echo /usr/bin/python3)"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@VENV@|$VENV_DIR|g" \
    -e "s|@SYSTEM_PYTHON@|$SYSTEM_PYTHON|g" \
    "$APP_DIR/bin/whisper-desk.in" > "$BIN"
chmod +x "$BIN"
ok "command installed: $BIN"

# --- 5. configuration -------------------------------------------------------
FIRST_INSTALL=0
if [ -f "$CONFIG" ]; then
    ok "existing configuration kept: $CONFIG"
else
    cp "$APP_DIR/config.example.toml" "$CONFIG"
    ok "configuration created: $CONFIG"
    FIRST_INSTALL=1
fi

# --- 5b. desktop entries ----------------------------------------------------
# Settings and History in the applications menu: the way in that works even
# where no tray icon is shown (GNOME without its AppIndicator extension).
if [ "$HOST" != macos ]; then
    mkdir -p "$APPLICATIONS_DIR"
    for entry in settings history; do
        sed -e "s|@BIN@|$BIN|g" "$APP_DIR/desktop/whisper-desk-$entry.desktop.in" \
            > "$APPLICATIONS_DIR/whisper-desk-$entry.desktop"
    done
    ok "Settings and History added to the applications menu"
fi
if [ "$HOST" = linux ]; then
    mkdir -p "$AUTOSTART_DIR"
    sed -e "s|@BIN@|$BIN|g" "$APP_DIR/desktop/whisper-desk-tray.desktop.in" \
        > "$AUTOSTART_DIR/whisper-desk-tray.desktop"
    ok "tray icon started at login (where the desktop shows one)"
fi

# --- 6. user service (automatic startup) ------------------------------------
SERVICE_INSTALLED=0
if [ "${WD_NO_SERVICE:-0}" != "1" ]; then
    if [ "$HOST" = macos ] && have launchctl; then
        say "Installing the launchd agent (started at login)"
        mkdir -p "$AGENT_DIR" "$STATE_DIR"
        AGENT="$AGENT_DIR/$AGENT_LABEL.plist"
        sed -e "s|@BIN@|$BIN|g" -e "s|@LOG@|$STATE_DIR/daemon.log|g" \
            "$APP_DIR/launchd/$AGENT_LABEL.plist.in" > "$AGENT"
        launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || true
        if launchctl bootstrap "gui/$(id -u)" "$AGENT" >/dev/null 2>&1 \
            || launchctl load -w "$AGENT" >/dev/null 2>&1; then
            ok "agent $AGENT_LABEL loaded"
            SERVICE_INSTALLED=1
        else
            warn "cannot load the agent — the daemon will start on demand"
        fi
    elif have systemctl && [ -d /run/systemd/system ]; then
        say "Installing the service (started at login)"
        mkdir -p "$UNIT_DIR"
        sed -e "s|@BIN@|$BIN|g" "$APP_DIR/systemd/whisper-desk.service.in" \
            > "$UNIT_DIR/whisper-desk.service"
        systemctl --user daemon-reload
        systemctl --user enable whisper-desk.service >/dev/null 2>&1 || warn "enable failed"
        systemctl --user restart whisper-desk.service >/dev/null 2>&1 || warn "startup deferred to the next session"
        ok "service whisper-desk.service enabled"
        SERVICE_INSTALLED=1
    else
        warn "neither systemd nor launchd — the daemon will be started on demand, on the first shortcut"
    fi
fi

# --- 7. keyboard shortcut ---------------------------------------------------
if [ "${WD_NO_HOTKEY:-0}" != "1" ]; then
    say "Installing the keyboard shortcut"
    "$BIN" hotkey install || warn "create the shortcut by hand on '$BIN toggle'"
fi

# --- 8. summary -------------------------------------------------------------
BINDING=$("$BIN" hotkey show 2>/dev/null | sed -n 's/.*"binding": "\(.*\)".*/\1/p' | head -1)
if [ -z "$BINDING" ]; then
    case "$HOST" in
        wsl) BINDING="Ctrl+Alt+J" ;;
        macos) BINDING="Cmd+J" ;;
        *) BINDING="<Super>j" ;;
    esac
fi
printf '\n%sinstallation complete.%s\n\n' "$BOLD" "$RESET"
printf '  Press %s%s%s, speak, then pause:\n' "$BOLD" "$BINDING" "$RESET"
printf '  the text is transcribed offline and inserted at the cursor.\n\n'
printf '  %sconfiguration%s  %s\n' "$DIM" "$RESET" "$CONFIG"
printf '  %sdiagnostic%s     whisper-desk doctor\n' "$DIM" "$RESET"
printf '  %sCLI dictation%s  whisper-desk record\n' "$DIM" "$RESET"
printf '  %supdate%s         whisper-desk update\n\n' "$DIM" "$RESET"
if [ "$SERVICE_INSTALLED" = 0 ] && [ "${WD_NO_SERVICE:-0}" != "1" ]; then
    printf '  %s!%s no service manager: the daemon starts on the first call\n\n' "$YELLOW" "$RESET"
fi
if [ "$HOST" = macos ]; then
    printf '  %s!%s macOS will ask for accessibility permission on the first paste\n' "$YELLOW" "$RESET"
    printf '     (System Settings → Privacy & Security → Accessibility).\n\n'
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf '  %s!%s add %s to your PATH: echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.bashrc\n\n' "$YELLOW" "$RESET" "$BIN_DIR" ;;
esac
printf '  %sThe model is downloaded on the first startup (a few hundred MB).%s\n\n' "$DIM" "$RESET"

# --- 9. first launch ----------------------------------------------------------
# A first installation ends on the welcome window: microphone, language and
# model, download, a first dictation. An update never reopens it.
if [ "$FIRST_INSTALL" = 1 ] && [ "${WD_NO_WELCOME:-0}" != "1" ] \
    && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || [ "$HOST" = macos ]; }; then
    nohup "$BIN" welcome --if-first >/dev/null 2>&1 &
    [ "$HOST" = linux ] && nohup "$BIN" tray >/dev/null 2>&1 &
    printf '  The welcome window is opening: it walks you through a first dictation.\n\n'
fi
