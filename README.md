![whisper-desk](https://raw.githubusercontent.com/SalvadorCardona/brand-assets/main/projects/whisper-desk/banner.png)

# whisper-desk

**Offline voice dictation for Linux, WSL and macOS.** You press `Super + J`, a small overlay
appears — a microphone and an equalizer dancing to your voice — and the text
**is typed straight where your cursor is**, sentence after sentence, while
you speak.

Each bar tracks a frequency band, from lows on the left to highs on the right: you
see the voice, not just its volume. The scale is in decibels and the fall is slower
than the rise, like a VU meter — silence leaves the bars at rest, an ordinary voice
sits in the middle of the range.

<p align="center">
  <img src="docs/overlay-listening.png" alt="Overlay while listening" width="262">
  &nbsp;&nbsp;
  <img src="docs/overlay-live.png" alt="Overlay showing the sentence just transcribed" width="408">
</p>

The overlay says at every moment what whisper-desk is doing, without opening a terminal:

| State | What you see |
|---|---|
| **loading** | the model is being loaded or downloaded — the equalizer turns into a gauge, with the percentage |
| **listening** | the equalizer follows your voice; below it, how to finish (`Super+J`) and to cancel (`Esc` or a click) |
| **live text** | in streaming mode, the sentence just transcribed appears under the equalizer, cut on the left |
| **working** | the last sentence is being transcribed |
| **done** | a tick and the beginning of the text inserted, for about a second |
| **error** | a short, readable reason — microphone unavailable or silent, nothing heard, text that could not be inserted — and what to do |
| **cancelled** | the dictation was cut off: nothing more is inserted |

<p align="center">
  <img src="docs/overlay-loading.png" alt="Overlay while the model downloads" width="267">
  &nbsp;
  <img src="docs/overlay-working.png" alt="Overlay while transcribing" width="262">
  <br>
  <img src="docs/overlay-done.png" alt="Overlay after a successful dictation" width="408">
  <br>
  <img src="docs/overlay-error.png" alt="Overlay after a dictation that heard nothing" width="408">
  <br>
  <img src="docs/overlay-cancelled.png" alt="Overlay after a cancelled dictation" width="408">
</p>

It fades in and out — unless animations are turned off on your desktop — and follows the
accent colour of the configuration.

No data leaves the machine: transcription runs on your GPU (or your CPU)
with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

📖 **[The documentation, online](https://cardona.digital/whisper-desk/)** — the same
text, but you can watch it work: a demo that dictates itself, the equalizer in motion,
and every table on a page that is pleasant to read.

<p align="center">
  <a href="https://cardona.digital/whisper-desk/">
    <img src="docs/site-hero.png" alt="The whisper-desk site: home page and live demo" width="760">
  </a>
</p>

<p align="center">
  <a href="https://cardona.digital/whisper-desk/#usage">
    <img src="docs/site-usage.png" alt="The site: the gestures of a dictation" width="376">
  </a>
  &nbsp;&nbsp;
  <a href="https://cardona.digital/whisper-desk/#how-it-works">
    <img src="docs/site-how-it-works.png" alt="The site: the architecture, explained" width="376">
  </a>
</p>

---

## Installation

```sh
curl -LsSf https://raw.githubusercontent.com/SalvadorCardona/whisper-desk/main/install.sh | sh
```

That's all. The script recognises the host — Linux, WSL or macOS — and adapts every step:

1. checks the system dependencies (microphone, clipboard, notifications, GTK) and offers
   to install them, with `apt` or `brew` as appropriate;
2. creates an isolated Python environment and installs faster-whisper in it — plus the
   CUDA libraries if an NVIDIA card is detected;
3. installs the `whisper-desk` command in `~/.local/bin`;
4. enables the user service — **systemd** on Linux and WSL, **launchd** on macOS —
   started automatically at login;
5. registers the global shortcut with the host's shortcut manager;
6. adds **Settings** and **History** to the applications menu, and the tray icon to the
   programs started at login (Linux);
7. on a first installation, opens the **welcome window**.

The Whisper model (a few hundred MB) is downloaded the first time the service starts. A
dictation started before it has arrived shows the download on the overlay, and the
microphone only opens once the model is ready.

### First launch

The welcome window walks through four steps: the microphone, with a gauge that must move
when you speak; the language and the model; the download, with its progress; then your
shortcut, and a first dictation into a text box. It opens once, after the first
installation (`WD_NO_WELCOME=1` skips it); `whisper-desk welcome` opens it again.

<p align="center">
  <img src="docs/welcome-microphone.png" alt="Welcome, step 1: the microphone gauge" width="400">
  &nbsp;
  <img src="docs/welcome-download.png" alt="Welcome, step 3: the model download" width="400">
  <br>
  <img src="docs/welcome-model.png" alt="Welcome, step 2: language and model" width="400">
  &nbsp;
  <img src="docs/welcome-try.png" alt="Welcome, step 4: the shortcut and a first dictation" width="400">
</p>

Once installed, updates go through the command itself: [`whisper-desk update`](#updating).

### What each host uses

| | Linux | WSL | macOS |
|---|---|---|---|
| **microphone capture** | `arecord` (ALSA) | `parec` (PulseAudio/WSLg) | `rec` (sox) or `ffmpeg` |
| **clipboard** | `wl-copy` / `xclip` | `clip.exe` | `pbcopy` |
| **paste keystroke** | `/dev/uinput` | SendKeys (PowerShell) | System Events (`osascript`) |
| **global shortcut** | GNOME (`gsettings`) | Start menu shortcut | `skhd`, or by hand |
| **service** | systemd | systemd, otherwise on demand | launchd |
| **notifications** | `notify-send` | `notify-send` (WSLg) | `osascript` |

None of these choices are set in stone: `backend`, `keyboard` and `paste_shortcut` can be
forced in the configuration.

> **Requirements** — a microphone, and depending on the host:
>
> - **Linux**: GNOME (Wayland or X11), `systemd` in the user session.
> - **WSL**: WSL 2 with WSLg (Windows 11, or an up-to-date Windows 10) for the microphone,
>   and Windows interoperability enabled. Text is inserted into **Windows** windows.
> - **macOS**: 12 or newer. The first paste asks for accessibility permission
>   (System Settings → Privacy & Security → Accessibility), and the first dictation asks
>   for microphone permission.
>
> An NVIDIA GPU is a bonus, not a requirement; on macOS transcription runs on the CPU,
> since CTranslate2 does not use Metal.

### Check that everything is in place

```sh
whisper-desk doctor
```

Each line is ✓ (fine), ✗ (to fix) or ⚠ (optional: whisper-desk works without), and every
failure comes with the line that fixes it:

```
System — Linux
  ✓ microphone capture — arecord, ffmpeg
  ✗ the microphone picks up sound ('default') — average level 0, peak 0
      → check the default source: wpctl status — or pick another one in whisper-desk settings
  ✓ clipboard — wl-copy
  ⚠ tray indicator — GNOME shows no tray icon without an extension
      → install and enable gnome-shell-extension-appindicator — meanwhile, Settings and History are in the applications menu

1 problem to fix — the → lines above say how.
```

---

## Updating

```sh
whisper-desk update
```

It says which version runs and what is available upstream, and stops there if there is
nothing new. Otherwise it fetches the sources, brings the Python environment up to date,
rewrites the command and the service, restarts the daemon, and hands back a summary of
what changed.

Your **configuration is kept**, and so is your **keyboard shortcut**: a shortcut you have
made your own is installed once, on the first installation, and never rewritten
afterwards. A dictation under way is never cut off — the update refuses to run and asks
you to come back once it is over.

```sh
whisper-desk update --check     # compares the versions and writes nothing
```

`--check` prints one line and exits **0** if the machine is up to date, **1** if something
newer exists upstream, and **2** if the question could not be answered (offline, private
repository, nonexistent branch). Enough to hang it on a cron or a status bar:

```sh
whisper-desk update --check
[ $? = 1 ] && notify-send "whisper-desk: an update is available"
```

`whisper-desk --version` and `whisper-desk doctor` both name the commit installed —
the project is distributed by `main`, so the commit says more than the version number.

> An installation made before this existed has no version fingerprint: `update` says so
> plainly and records one on the way through.

---

## Usage

| Gesture | Effect |
|---|---|
| `Super + J` | starts listening — the overlay appears |
| a short pause (~0.6 s) | the sentence is transcribed and **inserted at the cursor**, listening continues |
| 2 s of silence | end of dictation |
| `Super + J` (again) | stops listening immediately |
| `Super + J` (once more) | cuts the dictation off: what is left is neither transcribed nor inserted |
| `Super + J` (again and again) | gives up: the window closes, the microphone is released, the daemon is free |
| `Esc`, or a click on the overlay | cancels the dictation, like the third press — a click on a finished state dismisses it |

The default shortcut follows the host: `Super + J` on Linux and macOS (`Cmd + J`),
`Ctrl + Alt + J` on WSL — Windows reserves the Windows key for itself.

The text is inserted into the focused application: editor, browser, chat client,
search box. Your clipboard is handed back untouched at the end of the dictation.

`Esc` is caught without taking the focus away from your application, through the X11
server: under a Wayland session it only reaches whisper-desk while an X11 (Xwayland)
window has the focus. The shortcut and the click work everywhere.

### From the command line

```sh
whisper-desk record     # dictate and write the text to standard output
whisper-desk toggle     # same as the keyboard shortcut
whisper-desk status     # daemon state, loaded model, GPU or CPU
whisper-desk doctor     # full diagnostic
whisper-desk update     # update the installation (--check to compare only)
whisper-desk history    # past dictations — see below
whisper-desk settings   # the settings window — see Configuration
whisper-desk welcome    # the first-launch window, again
whisper-desk tray       # the tray icon — see below
whisper-desk pause      # the shortcut is ignored until 'whisper-desk resume'
whisper-desk config     # open the configuration in $EDITOR
whisper-desk reload     # reload the configuration without restarting
whisper-desk quit       # stop the daemon
```

### Tray icon

An icon in the system tray says whether the service runs, with which model and which
microphone, and holds a menu: start or finish a dictation, **History…**, **Settings…**,
**Pause dictation** (the shortcut then says it is paused instead of listening), **Quit**.
It starts with the session on Linux, and needs the AppIndicator library for the system
Python (`gir1.2-ayatanaappindicator3-0.1`).

**GNOME shows no tray icon by itself.** Without the
[AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/)
(`gnome-shell-extension-appindicator`, enabled by default on Ubuntu), the icon has nowhere
to appear — `whisper-desk doctor` says so. The alternative is already there: **whisper-desk
Settings** and **whisper-desk History** are in the applications menu, the overlay says
everything about a dictation, and `whisper-desk status` tells the service's state.

### History

Every dictation is kept — date, listening time, model and text — in
`~/.local/state/whisper-desk/history.jsonl`, one JSON line per dictation. A text lost on
the way (a window that lost the focus, an insertion that failed) can be found again,
copied or typed again. The `history.log` of earlier versions is still read, and folded
into the new file the first time the history is rewritten.

```sh
whisper-desk history                  # the 20 latest, numbered from 1 (the latest)
whisper-desk history invoice friday   # search: every word, case and accents ignored
whisper-desk history -n 0             # all of them
whisper-desk history --copy 3         # copies dictation 3 to the clipboard
whisper-desk history --delete 3       # deletes it
whisper-desk history --keep-days 30   # deletes dictations older than 30 days, from now on
whisper-desk history --clear          # deletes everything
whisper-desk history --window         # the window below
```

<p align="center">
  <img src="docs/history-window.png" alt="The history window: search, copy, insert again, delete" width="560">
</p>

The window lists the dictations newest first, with a search field (`Ctrl+F`), and for
each one **Copy**, **Insert** — the window closes and the daemon types the text where
your cursor was — and a bin that asks for a second click. At the bottom, *Keep
dictations* sets the automatic purge. It needs GTK3 for the system Python, like the
overlay; without it, the command line does the same.

---

## Configuration

The essentials are set in a window, without touching any file:

```sh
whisper-desk settings
```

<p align="center">
  <img src="docs/settings-window.png" alt="The settings window: language, model, microphone, shortcut, output, overlay" width="520">
</p>

Language; model, with its download size and an indication of its speed (and whether it is
already on disk); microphone, from the list of the capture tool's own sources, with a
**Test** gauge; shortcut, captured by pressing the new combination; output mode;
domain vocabulary; overlay position and colour. **Save** writes `config.toml` — only the
values that changed, your comments kept — reloads the daemon and, for a new shortcut,
installs it. Like the overlay, the window needs GTK3 for the system Python.

Everything, the rest included, is configurable in **`~/.config/whisper-desk/config.toml`**:

```toml
[hotkey]
binding = "auto"              # "auto", or <Ctrl>/<Alt>/<Shift>/<Super> + a key

[model]
name = "auto"                 # auto | tiny | base | small | medium | large-v3 | large-v3-turbo
device = "auto"               # auto | cuda | cpu
language = "fr"               # ISO code, or "auto" for detection
initial_prompt = ""           # vocabulary to favour (proper nouns, jargon)

[recording]
backend = "auto"              # auto | arecord | parec | rec | sox | ffmpeg
streaming = true              # insertion as you go, sentence by sentence
segment_silence_seconds = 0.6 # pause that splits a sentence
silence_seconds = 2.0         # silence that ends the dictation
max_seconds = 120

[output]
mode = "cursor"               # cursor | clipboard | stdout — combinable: "cursor+stdout"
paste_shortcut = "auto"       # "shift+insert" if you mostly dictate in a terminal
keyboard = "auto"             # auto | uinput | windows | applescript | none
restore_clipboard = true      # hands your original clipboard back at the end
notify = false
history = true                # keeps every dictation: whisper-desk history
history_days = 0              # dictations older than this are deleted; 0 = kept forever

[overlay]
enabled = true
accent = "#e46212"
position = "bottom-center"    # bottom-center | top-center | center
margin = 96
```

After a change by hand (the settings window does both for you):

```sh
whisper-desk reload            # for everything but the shortcut
whisper-desk hotkey install    # to apply a new shortcut
```

### Choosing a model

| Model | VRAM | Speed | Quality |
|---|---|---|---|
| `small` | ~1 GB | very fast | decent — default without a GPU |
| `medium` | ~2.5 GB | fast | good |
| `large-v3-turbo` | ~2 GB | fast | excellent — **default with a GPU** |
| `large-v3` | ~4.5 GB | slower | the best |

To shorten the delay between the end of a sentence and its insertion even further, lower
`beam_size` to `1` in `[model]`.

---

## How it works

```
Super + J  ─→  whisper-desk toggle  ─→  Unix socket  ─→  daemon (model in memory)
                                                              │
                              X11 overlay ←── audio level ────┤
                                                              │
 mic capture ─→ silence detection ─→ sentence ─→ faster-whisper ─→ text
                                                              │
                          clipboard + Ctrl+V (simulated keystroke) ─→ cursor
```

The daemon keeps the model loaded at all times, and transcribes one sentence while the
microphone is already recording the next. Only the two ends of that chain — capture and
keystroke — change from one host to another; the rest is shared.

Three Wayland constraints shaped this architecture:

- **A client cannot type into another client's window.** The `virtual-keyboard`
  protocol (the one `wtype` uses) is not implemented by GNOME. So we go through a
  kernel virtual keyboard (`/dev/uinput`, reachable without privileges thanks to the ACL
  set by systemd) that sends a plain paste shortcut.
- **Sending the text key by key would mean knowing the active XKB layout.**
  On an AZERTY keyboard, accents and half the letters would land on the wrong key; the
  paste shortcut, on the other hand, sits on the same physical key everywhere. The text
  therefore travels through the clipboard, which is restored afterwards.
- **A Wayland window can neither refuse focus nor position itself.** A Wayland overlay
  would catch the paste instead of your application. The overlay is therefore an X11
  client (via Xwayland) of type `NOTIFICATION`: never focused, and positionable.

The two other hosts follow the same idea with their own tools: on WSL, the keystroke goes
to Windows through `SendKeys` (a Linux virtual keyboard would only reach WSLg windows);
on macOS, it goes through System Events, which is what earns the program the accessibility
permission prompt.

---

## Troubleshooting

`whisper-desk doctor` starts by naming the detected host, then checks one by one the
pieces it uses: it is the first thing to try for everything below.

**The shortcut does nothing**
```sh
whisper-desk status                      # is the daemon answering?
whisper-desk hotkey show                 # is the shortcut registered?
journalctl --user -u whisper-desk -f     # the logs (systemd)
tail -f ~/.local/state/whisper-desk/daemon.log   # the logs (launchd, or a direct daemon)
```

**The text is not inserted but stays in the clipboard** — the simulated keystroke did not
get through. `whisper-desk doctor` says which one is at fault:

- **Linux** — `/dev/uinput` must be writable. In a local session, systemd grants you
  access automatically; over SSH or in a remote session, it does not.
- **macOS** — allow accessibility for the terminal (or for `whisper-desk`) in
  System Settings → Privacy & Security → Accessibility, then restart the daemon.
- **WSL** — `powershell.exe` must be reachable from WSL (interoperability enabled), and
  the target window must be a Windows window in the foreground.

**I dictate in a terminal and get `^V`** — `Ctrl+V` is not paste there. Set
`paste_shortcut = "shift+insert"` in `[output]`, then run `whisper-desk reload`.

**Sentences are cut too early / too late** — adjust `segment_silence_seconds`
(splitting) and `silence_seconds` (end of dictation) in `[recording]`.

**Someone is talking next to me and the dictation never ends** — in a shared office, a
nearby voice keeps the microphone busy: the silence that ends a dictation never comes.
The shortcut is a toggle and answers whatever the state — one press stops the listening,
the next one cuts the transcription off, and the conversation caught along the way is
dropped instead of being typed at the cursor.

**The overlay stays on screen and nothing responds any more** — a model stuck on a
segment, a frozen window, a microphone that no longer sends anything: keep pressing the
shortcut. Each press goes one step further, and the last one gives the dictation up
altogether — the window closes, the microphone is released, and the next press starts a
clean dictation. Should the daemon itself stop answering, the shortcut puts it down and
the following one starts a fresh daemon.

**The overlay opens but nothing is written** — nine times out of ten, the default
microphone is the wrong one (an empty jack socket often stays the default source, and
returns nothing but silence). `whisper-desk doctor` measures the level actually captured:

```sh
whisper-desk doctor          # "the microphone picks up sound" must be ticked
wpctl status                  # lists the sources; spot the real microphone
wpctl set-default <id>        # switch to it
```

The overlay now says so when a dictation captured no sound at all ("The microphone is
silent") — through a notification when there is no overlay — and the daemon logs the
measured peak:

```sh
journalctl --user -u whisper-desk | grep "peak"
```

**My voice is not loud enough** — raise the source gain (`wpctl set-volume <id> 1.0`),
or set the threshold by hand with `threshold = 400` (instead of `"auto"`) in `[recording]`.

**WSL: no sound arrives** — WSLg provides audio through PulseAudio; install
`pulseaudio-utils` (for `parec`), check that the microphone is allowed on the Windows side
(Settings → Privacy → Microphone) and that `pactl list sources short` does list an
`RDPSource` source.

**macOS: no sound arrives** — the first capture asks for microphone permission for the
terminal that starts the daemon; accept it, then check the default input in
System Settings → Sound. To pick another input with `ffmpeg`, list the indexes
(`ffmpeg -f avfoundation -list_devices true -i ""`) and put the number in
`device` with `backend = "ffmpeg"`.

**macOS: I don't want skhd for the shortcut** — create a Quick Action
(Automator → Run Shell Script, `~/.local/bin/whisper-desk toggle`) and assign it
a shortcut in System Settings → Keyboard → Keyboard Shortcuts → Services.

**Transcription runs on the CPU even though I have a GPU** — `whisper-desk status`
reports the selected device, and `journalctl --user -u whisper-desk` the reason for the
fallback (often missing CUDA libraries or not enough VRAM).

---

## Uninstalling

```sh
curl -LsSf https://raw.githubusercontent.com/SalvadorCardona/whisper-desk/main/uninstall.sh | sh
```

The menu entries and the tray icon go with it. Add `WD_PURGE=1` to remove the
configuration and the history as well. Downloaded models stay in `~/.cache/huggingface`.

---

## Development

```sh
git clone https://github.com/SalvadorCardona/whisper-desk
cd whisper-desk
WD_SRC="$PWD" sh install.sh     # installs from the local clone, without network access
```

An installation made this way remembers its clone: `whisper-desk update` replays it from
there, so a `git pull` in the clone is all it takes to update the machine.

### Tests

The suite depends on the standard library alone — no virtual environment, no model
to download:

```sh
python3 -m unittest discover -s tests -t .
```

It covers what can be checked without a microphone or a display server: sound level
measurement, sentence splitting, end of recording, configuration merging, output modes,
driving the listening window, and the choice of host-specific tools — the `WD_HOST`
environment variable (`linux`, `wsl`, `macos`) forces detection, which makes it possible
to test all three from any of them.

| File | Role |
|---|---|
| `src/whisper_desk/daemon.py` | service, Unix socket, listening and transcription in parallel |
| `src/whisper_desk/host.py` | host detection, PowerShell bridge under WSL |
| `src/whisper_desk/capture.py` | choice of capture tool (`arecord`, `parec`, `rec`, `ffmpeg`) |
| `src/whisper_desk/recorder.py` | silence detection, splitting into sentences |
| `src/whisper_desk/transcriber.py` | faster-whisper, GPU/CPU selection |
| `src/whisper_desk/inject.py` | simulated keystroke: `uinput`, SendKeys, System Events |
| `src/whisper_desk/output.py` | insertion at the cursor, clipboard, notifications |
| `src/whisper_desk/overlay.py` | X11 overlay (separate process) |
| `src/whisper_desk/overlay_protocol.py` | the line protocol between the daemon and the overlay |
| `src/whisper_desk/history.py` | dictation history (JSON Lines), search, purge |
| `src/whisper_desk/history_window.py` | history window (GTK3, separate process) |
| `src/whisper_desk/tomlwrite.py` | writing `config.toml` while keeping its comments |
| `src/whisper_desk/settings_window.py` | settings window (GTK3, separate process) |
| `src/whisper_desk/window_proc.py` | driving the GTK windows over JSON lines |
| `src/whisper_desk/welcome_window.py` | first-launch window (GTK3, separate process) |
| `src/whisper_desk/tray_window.py` | tray icon (AppIndicator, separate process) |
| `desktop/` | applications-menu entries and the tray's autostart entry |
| `src/whisper_desk/hotkey.py` | global shortcut: GNOME, Start menu, `skhd` |
| `src/whisper_desk/service.py` | daemon startup: systemd, launchd or direct |
| `src/whisper_desk/update.py` | version fingerprint, comparison with upstream, update |
| `tests/` | `unittest` suite, no external dependency |
| `install.sh` | installation and updates |

## License

MIT

## Author

Written and maintained by [Salvador Cardona, developer](https://cardona.digital) — thirteen
years of web development, and the other projects at the same address.
