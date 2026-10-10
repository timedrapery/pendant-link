# Pendant Link

A read-only Bluetooth connection test for the Limitless Pendant, running in Chrome on Android.

It connects to your own pendant, reads battery, recording state, storage, Wi-Fi, and firmware, and shows button presses as they happen. It never changes a setting, downloads audio, or touches the encryption key.

Live page: https://timedrapery.github.io/pendant-link/

## Using it

1. Open the live page in **Chrome on Android**. Web Bluetooth doesn't work inside embedded previews or in-app browsers.
2. Fully close the Limitless app (Settings → Apps → Limitless → Force stop). The pendant accepts only one connection at a time.
3. Turn on phone **Location** and **Bluetooth**. Android needs Location to scan for Bluetooth devices.
4. Tap the pendant to wake it, then tap **Connect** and pick it from the list. Allow pairing if asked.
5. If something fails, tap **Copy** under the log and share it.

If the pendant doesn't appear, try **Show all devices**, which drops the name filter.

## Files

| File | What it is |
| --- | --- |
| `index.html` | The page: UI, Web Bluetooth connection, rendering |
| `protocol.js` | Protocol logic with no DOM or Bluetooth: protobuf encode/decode, fragment reassembly, message decoding |
| `tests/protocol.test.js` | Tests for `protocol.js` |

## Tests

Requires Node 18 or newer. From the repo root:

```
node --test
```

## Protocol notes

- Service `632de001-…`; commands are written to `632de002-…` and responses arrive as notifications on `632de003-…`.
- Messages are protobuf, wrapped in an envelope with a message index, fragment number, and fragment count.
- Commands used here: `GetDeviceStatus` (field 21) and `GetDeviceInfo` (field 14).
- Battery also comes from the standard Bluetooth battery service (`0x180F`).

Protocol details come from the open-source [pendant-cli](https://github.com/sdelcore/pendant-cli) project and the Limitless Android app.

## Safety

- All text from the device or browser is rendered with `textContent`, never parsed as HTML.
- Packets are bounds-checked; malformed fragments are dropped and logged.

For personal use with your own device.
