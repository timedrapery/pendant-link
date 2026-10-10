# Raspberry Pi guide

This sets up a Raspberry Pi to look after your Limitless Pendant by itself: syncing every 15 minutes, encrypting with your own key, transcribing overnight, and (optionally) backing up to Google Drive. It takes about an hour. Every step is a command to copy and paste.

**Contents**

1. [What you need](#1-what-you-need)
2. [Flash the SD card](#2-flash-the-sd-card)
3. [Connect to the Pi](#3-connect-to-the-pi)
4. [Install pendant-cli](#4-install-pendant-cli)
5. [Pair the pendant](#5-pair-the-pendant)
6. [Automatic sync](#6-automatic-sync)
7. [Your own encryption key](#7-your-own-encryption-key)
8. [Transcripts](#8-transcripts)
9. [Nightly backup to Google Drive (optional)](#9-nightly-backup-to-google-drive-optional)
10. [Stars, the light, and a morning note](#10-stars-the-light-and-a-morning-note)
11. [Browse your days](#11-browse-your-days)

Then: [everyday commands](#everyday-commands) · [where things are](#where-things-are) · [troubleshooting](#troubleshooting) · [uninstall](#uninstall) · [what the patch changes](#what-the-pendant-cli-patch-changes)

**Typing commands:** paste one block at a time and press Enter. Anything in `<angle brackets>` is yours to fill in, without the brackets. In Windows Terminal, copy by highlighting text and right-clicking; **Ctrl+C means "stop"** there, not copy, and **Ctrl+Z pauses** a program rather than cancelling it.

**Before you start:** if you still use the Limitless app, export your data from Limitless first. This setup takes the pendant over completely.

---

## 1. What you need

- A **Limitless Pendant**, charged
- A **Raspberry Pi 4** (2 GB RAM is enough) and its power supply
- A **microSD card**, 32 GB or more. A recording uses about 7–11 MB an hour as compressed audio (plus a larger WAV copy for listening), so 128 GB leaves room to grow. A1 or A2 rated cards from SanDisk or Samsung hold up best.
- A **computer** to flash the card and type commands from, on the same Wi-Fi as the Pi
- Optional: a Google account, for the backup

## 2. Flash the SD card

1. On your computer, install **Raspberry Pi Imager** from [raspberrypi.com/software](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi 4**, then **Raspberry Pi OS Lite (64-bit)** (under "Raspberry Pi OS (other)"; Lite has no desktop, which a headless box doesn't need), then your SD card.
3. When it asks about settings, choose **Edit settings** and set:
   - a **hostname**, for example `pendantpi`
   - a **username and password**
   - your **Wi-Fi** name and password, and your country and time zone
   - on the **Services** tab, **enable SSH** with password authentication
4. Write the card, put it in the Pi, and power it on. Give it about two minutes the first time.

## 3. Connect to the Pi

Open a terminal on your computer (PowerShell on Windows, Terminal on Mac) and run, with the username and hostname you chose:

```
ssh <username>@<hostname>.local
```

Type `yes` if asked about the host's fingerprint, then your password. A prompt like `<username>@<hostname>:~ $` means you're on the Pi. Every command from here on runs there.

## 4. Install pendant-cli

[pendant-cli](https://github.com/sdelcore/pendant-cli) is the tool that talks to the pendant over Bluetooth. This project patches it, in step 6.

Update the system first, then reconnect after the restart:

```
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

Install what it needs:

```
sudo apt install -y git bluez protobuf-compiler libopus0 python3-venv python3-dbus python3-gi python3-protobuf python3-nacl python3-click python3-cryptography
```

Download it and build its message definitions:

```
cd ~ && git clone https://github.com/sdelcore/pendant-cli.git && cd pendant-cli && protoc -I proto --python_out=proto proto/pendant.proto
```

Give it its own Python environment and install it:

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install bleak opuslib
.venv/bin/pip install --no-deps --ignore-requires-python -e .
```

(The Pi ships a newer Python than pendant-cli lists, and two of its Bluetooth libraries are hard to build with pip; the system packages above supply them, and `--ignore-requires-python` skips the version check.)

Make `pendant` work from anywhere:

```
mkdir -p ~/.local/bin && ln -sf ~/pendant-cli/.venv/bin/pendant ~/.local/bin/pendant && cd ~
```

Log out (`exit`) and back in, then check:

```
pendant --help
```

A list of commands means it's installed.

## 5. Pair the pendant

The pendant pairs with one device at a time, so it has to forget your phone first.

1. **Close the Limitless app** on your phone (force stop it), or turn the phone's Bluetooth off.
2. **Factory-reset the pendant.** A plain long hold is *not* a reset. The sequence that works: hold the button until the light goes out; then tap, and immediately press and hold, until the light turns **purple**; release, and wait for **blue**. Do the next step while it shows blue.
3. Make sure the Pi's Bluetooth is on (it can start out blocked):

   ```
   sudo rfkill unblock all && sudo systemctl restart bluetooth
   ```

4. Pair, using `bluetoothctl`:

   ```
   bluetoothctl
   ```

   Then, at its `[bluetooth]#` prompt, one line at a time:

   ```
   power on
   agent NoInputNoOutput
   default-agent
   scan on
   ```

   Watch for a device named **Pendant** and note its address (like `AA:BB:CC:DD:EE:FF`). Then:

   ```
   scan off
   pair <address>
   trust <address>
   exit
   ```

5. Check it answers:

   ```
   export PENDANT_ADDRESS=<address>
   pendant status
   ```

   It shows battery, recording state and storage. (Run step 6 before trusting these numbers: unpatched pendant-cli misreads several of them.)

**After a factory reset the pendant records without encryption**, so anyone who pairs with it could read what's on it. Step 7 fixes that.

## 6. Automatic sync

This installs the pendant-cli patch, the `pendant sync` command and a timer that runs it every 15 minutes. It doesn't start the timer yet.

Still in the session where you ran `export PENDANT_ADDRESS=...` (or run it again):

```
curl -fsSL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/install-sync.sh | bash -s main
```

It ends with `==> Installed.` and saves your pendant's address, so you won't need `export` again. Try one sync by hand:

```
pendant sync
```

It downloads what's on the pendant, saves it in `~/recordings/<date>/`, and only then frees the space on the pendant. Check what it saved:

```
ls -R ~/recordings
```

Then turn on the timer, and let it keep running when you're not logged in:

```
systemctl --user enable --now pendant-sync.timer
sudo loginctl enable-linger "$USER"
```

Check it's scheduled: `systemctl --user list-timers` shows **pendant-sync.timer** with a NEXT time (press `q` to get your prompt back). If NEXT shows `-`, a sync is probably running right now; check again in a minute.

**How freeing works.** The sync frees pages only after the files are confirmed on disk, and only through the last page of an unbroken run it actually received. If a page went missing, everything after the gap stays on the pendant. If any audio can't be decrypted, nothing is freed.

## 7. Your own encryption key

The pendant can encrypt what it records so only your Pi can read it. This uses the same scheme the Limitless app used (P-256 keys and AES), with your key instead of Limitless's.

Pause the timer, and pull what's on the pendant now:

```
systemctl --user stop pendant-sync.timer
pendant sync
```

Set your key:

```
pendant crypto setkey
```

It should end with "The pendant accepted your key."

**Check it works before relying on it.** Talk near the pendant for half a minute, then download into a test folder. This doesn't free anything:

```
mkdir -p ~/enctest && pendant download --decode -o ~/enctest
```

You want `Loaded your audio key` near the top and **no** line saying `couldn't be decrypted`. Then clear the test folder and turn the timer back on:

```
rm -r ~/enctest
systemctl --user start pendant-sync.timer
```

**Back up your key.** Without it, recordings still on the pendant can't be read if the Pi's card fails. Show it as text:

```
echo "audio.key=$(base64 -w0 ~/.config/pendant/keys/audio.key)"
```

Save that line somewhere private, like a password manager. Keep it off the cloud service you use for the backup.

**Keep the Limitless app away from the pendant from now on.** If it connects, it may put its own key back. Nothing would be lost (those pages just stay on the pendant), but the Pi couldn't read them.

## 8. Transcripts

`pendant transcribe` turns recordings into text with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), on the Pi. No audio leaves the Pi to be transcribed. Each recording gets a `.txt` with one line per stretch of speech, stamped with the real time of day:

```
[13:47:27] Okay, so I'm supposed to be talking to you right now,
★ [14:16:28] I pressed the button right there.
```

Install it. This takes a few minutes and downloads a speech model of about 150 MB, once. The `HF_TOKEN` warning during the download is harmless and needs no account.

```
curl -fsSL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/install-transcribe.sh | bash -s main
```

Try it on one recording, then read it:

```
pendant transcribe --limit 1
cat $(ls -t ~/recordings/*/pendant_*.txt | head -1)
```

Turn on the nightly run (about 1:07 am, at low priority so syncing isn't held up):

```
systemctl --user enable --now pendant-transcribe.timer
```

On a Pi 4 it runs about 1.4× faster than real time for steady talking, and far faster through quiet stretches, so a full day finishes overnight. The pendant records only when it hears speech, so a quiet day gives a short transcript. That's expected.

**Tidying up.** WAVs are about 15 times bigger than the compact `.opus` audio. After each nightly run, WAVs from more than 14 days ago are deleted, but only when that recording's `.opus` and transcript are both there. To listen to an old one again, rebuild it: `pendant decode ~/recordings/<day>/<file>.opus`. Each run also logs how much space is free and warns when the card is getting full. Keep WAVs longer (or shorter) with `PENDANT_KEEP_WAV_DAYS=30` in `~/.config/pendant/env`, or set it to `never`. `pendant transcribe --tidy` tidies right away.

Settings, in `~/.config/pendant/env` or before the command: `PENDANT_WHISPER_MODEL` (`base.en` by default; `tiny.en` is faster, `small.en` more accurate but may not keep up on a Pi 4), `PENDANT_WHISPER_BEAM` (1; higher is slower and slightly more accurate).

## 9. Nightly backup to Google Drive (optional)

`pendant backup` copies `~/recordings` to a `pendant-backup` folder on your Google Drive every night at about 3:17 am:

- **Encrypted on the Pi first** with [rclone](https://rclone.org/), so Drive holds only scrambled names and contents.
- **Limited access.** rclone can see only the files it creates, not the rest of your Drive.
- **Copies, never mirrors.** Deleting something on the Pi doesn't delete the backup.
- **Skips** WAVs (they can be rebuilt from the compact audio with `pendant decode`), half-finished files and logs.

**Sign in to Google once.** Nothing gets installed on your computer: the Pi runs the sign-in and borrows your computer's browser through SSH. Leave the Pi (`exit`) and reconnect with one extra option:

```
ssh -L 53682:127.0.0.1:53682 <username>@<hostname>.local
```

Then install:

```
curl -fsSL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/install-backup.sh | bash -s main
```

It prints a link starting with `http://127.0.0.1:53682/auth`. Ctrl+click it (or copy it into your browser), sign in, and click **Allow**. **Don't type in the terminal while it says "Waiting for code..."**: it carries on by itself once you've clicked Allow.

Then run the first backup and turn on the timer:

```
pendant backup
systemctl --user enable --now pendant-backup.timer
```

**Back up the backup's passwords**, with your audio key. Without them the backup can't be read:

```
cat ~/.config/pendant/keys/backup-crypt.txt
```

**Restoring** on a new Pi: set up rclone with a Google sign-in and the same two passwords (`rclone config create pendant-backup crypt remote=gdrive:pendant-backup password=<password> password2=<password2> --obscure`), then `rclone copy pendant-backup: ~/recordings`.

**Readable transcripts (optional).** To let an assistant read your transcripts (see [the morning note](#a-morning-note)), add this setting, and the nightly backup also copies the `.txt` transcripts, **unencrypted**, to a `pendant-transcripts` folder on Drive. Text only, never audio. Google can read these too.

```
echo "PENDANT_TRANSCRIPTS_TO_DRIVE=yes" >> ~/.config/pendant/env
```

To stop, delete that line from `~/.config/pendant/env`. To remove what's already there, delete the folder on Drive.

## 10. Stars, the light, and a morning note

### Stars

**One quick press** of the pendant button marks a moment, as in the Limitless app. (Holding for 2 seconds still starts or stops recording.) The press is stored with the recording and its own clock time, so it works away from the Pi too. In the transcript, lines from about 90 seconds before the press to 15 seconds after it start with ★.

### The light

```
pendant led 0      # off
pendant led 10     # dim
pendant led 100    # full
pendant led        # show the current setting
```

With the light off, `pendant status` still tells you whether it's recording.

### A morning note

With readable transcripts on Drive (step 9), an AI assistant that can read your Google Drive can write you a short note each morning about the day before, starred moments first. [`morning-note.md`](morning-note.md) has a ready-made prompt to use as a daily scheduled task.

## 11. Browse your days

A page the Pi serves on your home network, for your phone or computer: your days, each day's transcript in order (split into conversations at long pauses), your stars highlighted, search across everything, and a page of just your starred moments. **Tap a line to hear it** from that moment. If a recording's WAV was tidied away, a button rebuilds it in a few seconds.

```
curl -fsSL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/install-web.sh | bash -s main
```

It prints the address to open (like `http://pendantpi.local:8765`, plus a numbered one in case `.local` names don't work on your phone) and a password. Any user name works with it, and your browser can remember it. The password is also in `~/.config/pendant/env`.

- It only uses Python's built-in parts, so nothing new is installed, and it starts by itself at boot.
- It's reachable only on your home network, and only with the password, because transcripts hold other people's words too. It's plain HTTP, so treat it as a home-only page; don't forward the port on your router.
- It doesn't log what anyone views.

---

## Everyday commands

| Command | What it does |
| --- | --- |
| `pendant status` | Battery, recording state, storage |
| `pendant sync` | Sync now, instead of waiting for the timer |
| `pendant transcribe` | Transcribe anything new now |
| `pendant transcribe --list` | Show recordings waiting for a transcript |
| `pendant transcribe --redo-day 2026-10-10` | Transcribe a whole day again |
| `pendant transcribe --tidy` | Delete old WAVs now and show free space |
| `pendant decode <file>.opus` | Rebuild a WAV to listen to |
| `pendant backup` | Back up now |
| `pendant led 0` | Light off (`100` is full) |
| `pendant crypto status` | Which keys are set up |
| `systemctl --user restart pendant-web` | Restart the browse page |
| `grep WEB_PASSWORD ~/.config/pendant/env` | Show the browse page's password |
| `systemctl --user list-timers` | When each job runs next (`q` to quit) |
| `tail -20 ~/recordings/sync.log` | What the last syncs did (also `transcribe.log`, `backup.log`) |

## Where things are

| Path | What's there |
| --- | --- |
| `~/recordings/YYYY-MM-DD/` | Each recording: `.opus` (compact audio), `.wav` (to listen to; deleted after 14 days, rebuildable), `.json` (times, stars, button presses), `.txt` (transcript), `.words.json` (transcript with exact times) |
| `~/recordings/*.log` | Logs for sync, transcription and backup |
| `~/.config/pendant/env` | Settings: your pendant's address, options |
| `~/.config/pendant/keys/` | Your encryption keys and backup passwords. Keep a copy elsewhere. |
| `~/.local/share/pendant/` | The sync, backup and transcription scripts, and the transcription environment |
| `~/.config/systemd/user/pendant-*` | The three timers and their jobs, and the browse page |

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `pendant status` can't find or connect to the pendant | Tap the pendant to wake it and keep it near the Pi. Check Bluetooth isn't blocked: `rfkill list bluetooth` should say `Soft blocked: no`; if not, `sudo rfkill unblock all && sudo systemctl restart bluetooth`. |
| Pairing fails (`AuthenticationFailed`, or `Pairing Failed, reason 0x08` in `btmon`) | The pendant still remembers your phone. Close the Limitless app and do the full factory reset in step 5 (to purple, then blue). A long hold alone isn't a reset. |
| `Protobuf not available` | Rebuild the message definitions: `cd ~/pendant-cli && protoc -I proto --python_out=proto proto/pendant.proto && cd ~` |
| `A sync is already running; skipping` | The 15-minute sync is busy. Wait a minute and try again. |
| `list-timers` shows `-` under NEXT | A run is in progress. Check again when it's done. |
| `No such command 'sync'` (or `backup`, `led`, `transcribe`) | The patch isn't applied: run the step 6 install command again. It's safe to rerun any installer. |
| `couldn't be decrypted` | Nothing is lost: those pages stay on the pendant, and the chunks are saved whole in a `.enc.jsonl` file. Make sure the Limitless app hasn't connected. Decrypt saved chunks later with `pendant crypto decrypt <file>.enc.jsonl`. |
| Yellow `Retrying ...` warnings while installing | A network hiccup; pip retries by itself. If it ends in a red `ERROR`, run the same command again. |
| `journalctl --user` says `No journal files were found` | Raspberry Pi OS keeps those logs elsewhere. Use the plain logs: `tail -20 ~/recordings/sync.log`. |
| The Google sign-in page won't open, or "Didn't get a token" | Reconnect with `ssh -L 53682:127.0.0.1:53682 ...` (step 9) and run the installer again. Click Allow once, and don't type in the terminal while it waits. |
| A program shows `Stopped` after Ctrl+Z | Ctrl+Z pauses rather than cancels. End it with `kill %1`. |
| Transcripts have very few lines | The pendant records only while it hears speech, so quiet time leaves little to transcribe. Talk near it and run `pendant sync` then `pendant transcribe` to check. |
| Transcript times look off for recordings from before you updated | Redo the day: `pendant transcribe --redo-day <YYYY-MM-DD>`. |
| The browse page won't open | Use the numbered address the installer printed (`hostname -I` shows it) if `.local` doesn't work on your phone. Check it's running: `systemctl --user status pendant-web --no-pager`. |
| The light came back on | Run `pendant led 0` again. |
| "Getting full" in `transcribe.log` | Lower `PENDANT_KEEP_WAV_DAYS` in `~/.config/pendant/env`, or move old day folders off the Pi (they're in the backup if you set it up). |

## Uninstall

```
systemctl --user disable --now pendant-sync.timer pendant-transcribe.timer pendant-backup.timer pendant-web.service
rm -f ~/.config/systemd/user/pendant-* && systemctl --user daemon-reload
rm -rf ~/.local/share/pendant ~/pendant-cli ~/.local/bin/pendant
```

That leaves your recordings (`~/recordings`) and keys (`~/.config/pendant`) alone; delete them yourself if you mean to. A factory reset of the pendant (step 5) clears your key from it, after which it records unencrypted again.

---

## What the pendant-cli patch changes

`pendant-cli-fixes.patch` fixes how [pendant-cli](https://github.com/sdelcore/pendant-cli) reads the pendant and adds what this guide uses. Message layouts come from the Limitless Android app. It applies cleanly to upstream commit `1c71659` and passes pendant-cli's tests (90, including 11 for the page-freeing logic and 13 for encryption). The installer in step 6 applies it; it resets pendant-cli to its original files first, so it's safe to rerun.

**Reading the pendant correctly**
- **Recording status** shows the real state: not recording, recording, or listening (armed to record when it hears speech), which pendant-cli used to print as `False`.
- **Storage** shows pages used out of total, with one decimal of percent (it used to round small amounts down to 0%).
- **Button events** show the press type (short, double, long, short plus super-long) and press counts.
- **Device info** (`pendant info`) shows firmware, device ID, Bluetooth address in normal byte order, and whether the pendant holds its own public key (its own key, so that alone doesn't mean recordings are encrypted). It leaves out fields the pendant sends blank, and waits up to 10 seconds for the reply.
- **Wi-Fi status**, **command replies** (`ResponseData`) and **`SetLed`** use the app's layouts. Messages that fail to decode are printed instead of silently dropped.

**Encryption, rebuilt to match the app.** pendant-cli assumed X25519 keys and a NaCl sealed box. The app (read from its JavaScript bundle) uses P-256 ECDH, HKDF-SHA256 with empty salt and info, and AES-128-GCM, and the encrypted-audio message is nonce (field 1), ciphertext (2), tag (3), packet offsets (4); pendant-cli had nonce and ciphertext swapped. `pendant crypto setkey`, `status` and `decrypt` now follow the app. The key derivation was checked against WebCrypto, the API the app uses.

**Downloading safely.** `pendant download` stops once the pendant goes quiet (`--idle-timeout`) and can free pages afterwards (`--delete-after`): only after the files are confirmed on disk, only through the end of an unbroken run, and never if any audio couldn't be decrypted.

**New commands:** `pendant sync`, `pendant backup`, `pendant transcribe` (which run the scripts in this folder), and `pendant led`.

**Stars and clock times.** Button presses stored in the recording come through with their own clock time (`button_events` and `stars_ms` in each download's `.json`). Decoding saves a `timeline` that ties positions in the audio to the real time of day, since the pendant records only while it hears speech; transcripts use it.

**Small fixes:** the docs' `protoc` command gains `-I proto`, so the generated file lands in `proto/`.

## Developing

- pendant-cli's tests, with the patch applied: `cd ~/pendant-cli && python -m pytest`
- Transcription and browse-page tests (a stand-in model, no audio needed): `pip install pytest numpy faster-whisper`, then `pytest pi/tests`
- The installers take a commit as their argument (`bash -s <commit>`), so you can pin an exact version. `main` is the latest; GitHub may serve a version up to 5 minutes old right after a change.
