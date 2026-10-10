# Raspberry Pi notes

## pendant-cli fixes

`pendant-cli-fixes.patch` corrects how [pendant-cli](https://github.com/sdelcore/pendant-cli) reads three pendant messages, using the field layouts from the Limitless Android app:

- **Recording status** shows the real state: not recording, recording, or listening. The pendant reports "listening" when it's armed to record on speech, which pendant-cli used to print as `False`.
- **Storage** shows pages used out of total, with one decimal of percent. It used to round small amounts down to 0%.
- **Button events** show the press type (short, double, long, short plus super-long) and press counts since boot.
- **Device info** (`pendant info`) shows firmware, serial, device ID, Bluetooth address (in normal byte order), flash pages, and whether the pendant holds its own public key. That key is the pendant's, so its presence doesn't mean recordings are encrypted.
- **Wi-Fi status** uses the real layout (state, signal, uptime, network name).
- Status updates that leave out the recording state no longer print "unknown".
- `pendant info` waits for the pendant's reply (asking again once if needed, up to 10 seconds) instead of giving up after 3.
- Messages that fail to decode are printed instead of silently dropped.
- The docs' `protoc` command gains `-I proto`, so the generated file lands in `proto/`.

It applies cleanly to upstream commit `1c71659` and passes pendant-cli's test suite (55 tests).

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
