// Pendant protocol helpers: protobuf encode/decode, BLE fragment reassembly,
// and decoding of pendant messages into plain objects.
// No DOM or Bluetooth here, so it runs unchanged in the browser and under Node tests.
(function (root) {
  "use strict";

  // ---------- writer ----------
  function varint(n) {
    if (!Number.isSafeInteger(n) || n < 0) throw new RangeError("varint needs a non-negative safe integer");
    const out = [];
    while (n > 127) { out.push((n % 128) | 0x80); n = Math.floor(n / 128); }
    out.push(n);
    return out;
  }
  const tag = (num, wire) => varint(num * 8 + wire);
  const lenField = (num, bytes) => [...tag(num, 2), ...varint(bytes.length), ...bytes];
  const vField = (num, val) => [...tag(num, 0), ...varint(val)];

  // ServerCommandMsg with one empty command sub-message plus RequestData,
  // wrapped in a single-fragment BLEMessageFromNativeToPendant envelope.
  function encodeCommand(cmdField, msgIndex, requestId) {
    const requestData = vField(1, requestId);                              // RequestData{request_id}
    const cmd = [...lenField(cmdField, []), ...lenField(30, requestData)];  // ServerCommandMsg
    const env = [...vField(1, msgIndex), ...vField(2, 0), ...vField(3, 1), ...lenField(4, cmd)];
    return new Uint8Array(env);
  }

  // ---------- reader ----------
  function readVarint(b, i) {
    let shift = 0, res = 0, byte;
    do {
      if (i >= b.length || shift > 63) throw new Error("truncated varint");
      byte = b[i++];
      res += (byte & 0x7f) * Math.pow(2, shift);
      shift += 7;
    } while (byte & 0x80);
    return [res, i];
  }

  // Returns {fieldNumber: [values]}; length-delimited values are Uint8Array slices.
  function parseFields(b) {
    const f = {};
    let i = 0;
    while (i < b.length) {
      let tg; [tg, i] = readVarint(b, i);
      const num = Math.floor(tg / 8), wire = tg % 8;
      let val;
      if (wire === 0) { [val, i] = readVarint(b, i); }
      else if (wire === 2) {
        let len; [len, i] = readVarint(b, i);
        if (i + len > b.length) throw new Error("field overruns message");
        val = b.slice(i, i + len); i += len;
      }
      else if (wire === 5) { if (i + 4 > b.length) throw new Error("truncated fixed32"); val = b.slice(i, i + 4); i += 4; }
      else if (wire === 1) { if (i + 8 > b.length) throw new Error("truncated fixed64"); val = b.slice(i, i + 8); i += 8; }
      else throw new Error("unsupported wire type " + wire);
      (f[num] = f[num] || []).push(val);
    }
    return f;
  }
  const first = (f, n) => (f[n] ? f[n][0] : undefined);
  const asStr = (u8) => {
    if (!(u8 instanceof Uint8Array)) return "";
    try { return new TextDecoder().decode(u8); } catch (e) { return ""; }
  };
  const sub = (f, n) => { const v = first(f, n); return v instanceof Uint8Array ? parseFields(v) : null; };

  // ---------- envelope + reassembly ----------
  function parseEnvelope(bytes) {
    const env = parseFields(bytes);
    return {
      index: first(env, 1) || 0,
      seq: first(env, 2) || 0,
      count: first(env, 3) || 1,
      payload: first(env, 4),
    };
  }

  class Reassembler {
    constructor({ maxFragments = 64, maxPending = 8 } = {}) {
      this.maxFragments = maxFragments;
      this.maxPending = maxPending;
      this.pending = new Map();
    }
    // Returns the complete payload, or null while a message is still arriving.
    // Throws on malformed fragments so the caller can log and drop them.
    push({ index, seq, count, payload }) {
      if (!(payload instanceof Uint8Array)) throw new Error("fragment has no payload");
      if (!Number.isInteger(count) || count < 1 || count > this.maxFragments) throw new Error("bad fragment count " + count);
      if (!Number.isInteger(seq) || seq < 0 || seq >= count) throw new Error("bad fragment seq " + seq + " of " + count);
      if (count === 1) return payload;

      let parts = this.pending.get(index);
      if (parts && parts.length !== count) { this.pending.delete(index); parts = null; } // count changed mid-message
      if (!parts) {
        if (this.pending.size >= this.maxPending) this.pending.clear();                // drop stale partials
        parts = new Array(count).fill(null);
        this.pending.set(index, parts);
      }
      parts[seq] = payload;
      if (!parts.every(Boolean)) return null;

      this.pending.delete(index);
      const full = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      let o = 0;
      for (const p of parts) { full.set(p, o); o += p.length; }
      return full;
    }
    reset() { this.pending.clear(); }
  }

  // ---------- message decoding ----------
  const BUTTON = { 0: "none", 1: "short press", 2: "long press", 3: "double press", 4: "short + super-long press" };
  const RECORDING = { 0: "idle", 1: "recording", 2: "listening" };
  const BATTERY = { 0: "unknown", 1: "charging", 2: "on battery", 3: "not charging", 4: "full" };

  function decodeBattery(b) {
    if (!b) return null;
    const state = first(b, 1) || 0;
    return {
      percent: first(b, 5),
      state: BATTERY[state] || "unknown",
      usbConnected: first(b, 9) === 2,
    };
  }

  // PendantAllMsg -> {deviceInfo?, status?, battery?, button?}
  function decodePendantMessage(payload) {
    const f = parseFields(payload);
    const out = {};

    const info = sub(f, 1);                                   // DeviceInfoMsg
    if (info) out.deviceInfo = { firmware: asStr(first(info, 3)) || "?", serial: asStr(first(info, 2)) || "?" };

    const st = sub(f, 5);                                     // DeviceStatus
    if (st) {
      const status = {};
      const rec = sub(st, 7);
      if (rec) { const code = first(rec, 1) || 0; status.recording = { code, label: RECORDING[code] || "unknown" }; }
      const store = sub(st, 5);
      if (store) {
        const free = first(store, 4) || 0, total = first(store, 5) || 0;
        if (total > 0) status.storage = { used: total - free, total, percent: Math.round((total - free) / total * 100) };
      }
      const wifi = sub(st, 1);
      if (wifi) status.wifi = { connected: !!first(wifi, 1), ssid: asStr(first(wifi, 2)) };
      const batt = decodeBattery(sub(st, 9));
      if (batt) status.battery = batt;
      out.status = status;
    }

    const batt = decodeBattery(sub(f, 7));                    // BatteryStatus
    if (batt) out.battery = batt;

    const btn = sub(f, 8);                                    // ButtonStatus
    if (btn) { const code = first(btn, 1) || 0; out.button = { code, label: BUTTON[code] || "event " + code }; }

    return out;
  }

  const api = {
    varint, tag, lenField, vField, encodeCommand,
    readVarint, parseFields, parseEnvelope, Reassembler, decodePendantMessage,
    COMMANDS: { GET_DEVICE_INFO: 14, GET_DEVICE_STATUS: 21 },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PendantProtocol = api;
})(typeof self !== "undefined" ? self : this);
