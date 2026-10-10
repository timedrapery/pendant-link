# Pendant Link

Keep your **Limitless Pendant** working after Limitless shuts down, on a Raspberry Pi you own. No Limitless account, app or servers.

Limitless was acquired by Meta in December 2025, and support for the pendant ends after 2026. The pendant itself is a good wearable microphone with a button, a light and about 37 hours of storage. This project talks to it directly over Bluetooth and does what the Limitless app used to do, on your own hardware.

## What you get

- **Automatic sync.** Every 15 minutes the Pi pulls new recordings off the pendant whenever it's in range, files them by day, and only then frees the space on the pendant. If anything goes wrong, nothing is deleted.
- **Your own encryption key.** The pendant encrypts what it records so only your Pi can read it (the same scheme the Limitless app used, with your key instead of theirs).
- **Transcripts made on the Pi.** Each night the recordings become text with real clock times, using an open speech-to-text model that runs on the Pi. No audio is sent anywhere to be transcribed.
- **Stars.** One quick press of the pendant button marks a moment ("keep this"), as in the Limitless app. Lines around it are starred in the transcript.
- **Nightly encrypted backup** to Google Drive (optional). It's encrypted on the Pi first, so Google can't read it.
- **Browse your days** from your phone on your home Wi-Fi: each day's transcript, your stars, search, and tap a line to hear it.
- **The light** can be dimmed or turned off: `pendant led 0`.
- Optionally, readable transcripts on Drive so an assistant like Claude can read them, for example to write you a short note each morning about the day before.

## How it works

```
 Pendant ──Bluetooth──▶ Raspberry Pi ──(optional)──▶ Google Drive
 records on speech      every 15 min: sync           3:17 am: encrypted backup
 encrypts with          1:07 am: transcribe          (+ readable transcripts,
 your key               files everything by day       only if you turn it on)
```

Everything runs on the Pi, by itself, and comes back after a reboot.

## Get started

**[The step-by-step guide is in `pi/README.md`](pi/README.md).** It takes about an hour, starting from a blank SD card. You'll need:

- a Limitless Pendant
- a Raspberry Pi 4 (2 GB is enough), its power supply, and a microSD card (32 GB or more; 128 GB gives room to grow)
- a computer to flash the card and type commands from (Windows, Mac or Linux)

No programming is needed: every step is a command to copy and paste.

## Be kind with it

This was built on one rule: make only what is kind to the person wearing it and to the people around them, worth doing now, and made with care for whoever comes later. Anything that would quietly turn the people nearby into data is out.

In practice:

- **Tell people.** The pendant records conversations, including other people's voices. Recording laws vary: many places, including several US states, require everyone's consent. Know your local law, and be open with the people around you.
- **Your data stays yours.** Recordings and transcripts live on your Pi. The backup is encrypted before it leaves. Readable transcripts go to the cloud only if you turn that on, and the guide says plainly who can read them.
- **Nothing here identifies, profiles or tracks other people**, and contributions shouldn't either.

## What's in this repository

| Path | What it is |
| --- | --- |
| [`pi/`](pi/) | Everything for the Raspberry Pi: the guide, installers, sync, backup and transcription |
| [`pi/pendant-cli-fixes.patch`](pi/pendant-cli-fixes.patch) | Fixes and additions for [pendant-cli](https://github.com/sdelcore/pendant-cli), the Bluetooth tool this builds on |
| [`pi/morning-note.md`](pi/morning-note.md) | Optional: a recipe for a daily note from your transcripts |
| `index.html`, `protocol.js` | An earlier, read-only connection test that runs in Chrome on Android ([below](#android-connection-test)) |

## Credits

- [pendant-cli](https://github.com/sdelcore/pendant-cli) by sdelcore reverse-engineered the pendant's Bluetooth protocol. This project patches it, using message layouts read from the Limitless Android app, to fix how several messages are read and to add encryption, syncing, stars and the light.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) does the transcription; [rclone](https://rclone.org/) does the backup.

Not affiliated with Limitless or Meta.

## License

MIT for the code and docs written here (see [LICENSE](LICENSE)). The pendant-cli patch follows pendant-cli's own terms: personal and research use only.

---

## Android connection test

`index.html` is a read-only Bluetooth test for Chrome on Android, from before this moved to a Raspberry Pi. It connects to your pendant and shows battery, recording state, storage, Wi-Fi, firmware and live button presses. It never changes a setting or downloads audio.

Live page: https://timedrapery.github.io/pendant-link/

1. Open the page in **Chrome on Android** (Web Bluetooth doesn't work in embedded previews or in-app browsers).
2. Fully close the Limitless app (Settings → Apps → Limitless → Force stop). The pendant accepts one connection at a time.
3. Turn on **Location** and **Bluetooth**; Android needs Location to scan.
4. Tap the pendant to wake it, tap **Connect**, and pick it. If it doesn't appear, try **Show all devices**.

Tests (Node 18+): `node --test`. Protocol notes: service `632de001-…`; commands go to `632de002-…`, responses arrive as notifications on `632de003-…`; messages are protobuf in an envelope with an index, fragment number and fragment count.
