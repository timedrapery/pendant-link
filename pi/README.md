# Raspberry Pi notes

## pendant-cli fixes

`pendant-cli-fixes.patch` corrects how [pendant-cli](https://github.com/sdelcore/pendant-cli) reads three pendant messages, using the field layouts from the Limitless Android app:

- **Recording status** shows the real state: not recording, recording, or listening. The pendant reports "listening" when it's armed to record on speech, which pendant-cli used to print as `False`.
- **Storage** shows pages used out of total, with one decimal of percent. It used to round small amounts down to 0%.
- **Button events** show the press type (short, double, long, short plus super-long) and press counts since boot.
- **Device info** (`pendant info`) shows firmware, serial, device ID, Bluetooth address (in normal byte order), flash pages, and whether the pendant holds its own public key, leaving out fields the pendant sends blank (like serial number). That key is the pendant's, so its presence doesn't mean recordings are encrypted.
- **Wi-Fi status** uses the real layout (state, signal, uptime, network name).
- Status updates that leave out the recording state no longer print "unknown".
- `pendant info` waits for the pendant's reply (asking again once if needed, up to 10 seconds) instead of giving up after 3.
- New `pendant sync` command runs the automatic sync below.
- **Encryption rebuilt to match the Limitless app.** pendant-cli assumed X25519 keys and a NaCl sealed box; the app (read from its JavaScript bundle) uses P-256 ECDH, HKDF-SHA256 with empty salt and info, and AES-128-GCM. `pendant crypto setkey` now does that, checked against WebCrypto. Audio that can't be decrypted is saved whole (`.enc.jsonl`) and its pages are never freed.
- The pendant's command replies (`ResponseData`) use the app's layout, so errors are reported.
- Messages that fail to decode are printed instead of silently dropped.
- `pendant download` stops once the pendant goes quiet (`--idle-timeout`, default 8 s) and can free pages afterward (`--delete-after`). It frees only after the saved files are confirmed on disk, and only through the last page in an unbroken run it received, so a gap is never deleted.
- The docs' `protoc` command gains `-I proto`, so the generated file lands in `proto/`.

It applies cleanly to upstream commit `1c71659` and passes pendant-cli's test suite (79 tests, including 11 for the freeing logic and 11 for encryption).

### Apply on the Pi

```
cd ~/pendant-cli
git checkout -- .
curl -sL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/pendant-cli-fixes.patch | git apply
protoc -I proto --python_out=proto proto/pendant.proto
cd ~
```

`git checkout -- .` resets pendant-cli to its original files first, so this works whether or not an earlier version of the patch was applied. The `protoc` line regenerates the message definitions, which the patch changes. Then try `pendant status`, `pendant info`, and `pendant listen`.

To undo: `cd ~/pendant-cli && git checkout -- . && protoc -I proto --python_out=proto proto/pendant.proto`

## Automatic sync

`pendant sync` pulls new recordings, files them in `~/recordings/YYYY-MM-DD/`, and frees only the pages it saved. If the pendant is out of range it skips quietly, and only one sync runs at a time. The timer runs it every 15 minutes.

Install (applies the patch above too; doesn't start the timer):

```
export PENDANT_ADDRESS=AA:BB:CC:DD:EE:FF
curl -fsSL https://raw.githubusercontent.com/timedrapery/pendant-link/main/pi/install-sync.sh | bash -s main
```

Run `pendant sync` once by hand and check `~/recordings`, then turn on the timer:

```
systemctl --user enable --now pendant-sync.timer
sudo loginctl enable-linger "$USER"   # keeps it running when you're not logged in
```

Log: `~/recordings/sync.log` (or `systemctl --user status pendant-sync.service`). To stop: `systemctl --user disable --now pendant-sync.timer`.

## Your own encryption key

```
systemctl --user stop pendant-sync.timer   # pause while setting up
pendant sync                               # pull what's on the pendant now
pendant crypto setkey
```

Then speak near the pendant for half a minute and check it decrypts, without freeing anything:

```
mkdir -p ~/enctest && pendant download --decode -o ~/enctest
```

It should say "Loaded your audio key", and no "couldn't be decrypted" line. Back up `~/.config/pendant/keys/`, then `systemctl --user start pendant-sync.timer`.
