// Run from the repo root with: node --test
const test = require("node:test");
const assert = require("node:assert/strict");
const P = require("../protocol.js");

// Build a pendant->phone envelope around a payload (mirrors the phone->pendant layout).
function envelope({ index = 0, seq = 0, count = 1, payload }) {
  return new Uint8Array([
    ...P.vField(1, index), ...P.vField(2, seq), ...P.vField(3, count), ...P.lenField(4, [...payload]),
  ]);
}
const msg = (...fields) => new Uint8Array(fields.flat());
const str = (s) => [...new TextEncoder().encode(s)];

test("varint round-trips small and multi-byte values", () => {
  for (const n of [0, 1, 127, 128, 300, 16384, 2 ** 40]) {
    const bytes = new Uint8Array(P.varint(n));
    assert.deepEqual(P.readVarint(bytes, 0), [n, bytes.length]);
  }
});

test("encodeCommand produces the expected GetDeviceStatus bytes", () => {
  const bytes = P.encodeCommand(P.COMMANDS.GET_DEVICE_STATUS, 0, 1);
  // envelope: index=0, seq=0, count=1, payload = [field21 empty][field30 {request_id=1}]
  assert.deepEqual([...bytes], [
    0x08, 0x00, 0x10, 0x00, 0x18, 0x01, 0x22, 0x08,
    0xaa, 0x01, 0x00,             // field 21, length 0
    0xf2, 0x01, 0x02, 0x08, 0x01, // field 30, RequestData{request_id: 1}
  ]);
});

test("parseFields rejects truncated and overrunning input", () => {
  assert.throws(() => P.parseFields(new Uint8Array([0x08])), /truncated varint/);
  assert.throws(() => P.parseFields(new Uint8Array([0x0a, 0x05, 0x01])), /overruns/);
  assert.throws(() => P.parseFields(new Uint8Array([0x0d, 0x01])), /fixed32/);
});

test("reassembler joins fragments in any order", () => {
  const r = new P.Reassembler();
  const a = new Uint8Array([1, 2]), b = new Uint8Array([3, 4]);
  assert.equal(r.push({ index: 7, seq: 1, count: 2, payload: b }), null);
  assert.deepEqual([...r.push({ index: 7, seq: 0, count: 2, payload: a })], [1, 2, 3, 4]);
  assert.equal(r.pending.size, 0);
});

test("reassembler rejects malformed fragments", () => {
  const r = new P.Reassembler({ maxFragments: 4 });
  const payload = new Uint8Array([1]);
  assert.throws(() => r.push({ index: 1, seq: 2, count: 2, payload }), /seq/);
  assert.throws(() => r.push({ index: 1, seq: -1, count: 2, payload }), /seq/);
  assert.throws(() => r.push({ index: 1, seq: 0, count: 0, payload }), /count/);
  assert.throws(() => r.push({ index: 1, seq: 0, count: 1e9, payload }), /count/);
  assert.throws(() => r.push({ index: 1, seq: 0, count: 2, payload: undefined }), /payload/);
});

test("reassembler drops stale partials instead of growing forever", () => {
  const r = new P.Reassembler({ maxPending: 2 });
  const p = new Uint8Array([0]);
  r.push({ index: 1, seq: 0, count: 2, payload: p });
  r.push({ index: 2, seq: 0, count: 2, payload: p });
  r.push({ index: 3, seq: 0, count: 2, payload: p });
  assert.ok(r.pending.size <= 2);
});

test("decodes a DeviceStatus with recording, storage, wifi, and battery", () => {
  const recording = P.vField(1, 2);                                   // listening
  const storage = [...P.vField(4, 750), ...P.vField(5, 1000)];       // free, total
  const wifi = [...P.vField(1, 1), ...P.lenField(2, str("home"))];
  const battery = [...P.vField(1, 1), ...P.vField(5, 83), ...P.vField(9, 2)];
  const status = [
    ...P.lenField(7, recording), ...P.lenField(5, storage),
    ...P.lenField(1, wifi), ...P.lenField(9, battery),
  ];
  const payload = msg(P.lenField(5, status));
  const env = P.parseEnvelope(envelope({ payload }));
  const out = P.decodePendantMessage(new P.Reassembler().push(env));

  assert.deepEqual(out.status.recording, { code: 2, label: "listening" });
  assert.deepEqual(out.status.storage, { used: 250, total: 1000, percent: 25 });
  assert.deepEqual(out.status.wifi, { connected: true, ssid: "home" });
  assert.deepEqual(out.status.battery, { percent: 83, state: "charging", usbConnected: true });
});

test("decodes device info and button events", () => {
  const info = [...P.lenField(2, str("SN123")), ...P.lenField(3, str("1.2.3"))];
  assert.deepEqual(P.decodePendantMessage(msg(P.lenField(1, info))).deviceInfo, { firmware: "1.2.3", serial: "SN123" });

  const button = P.decodePendantMessage(msg(P.lenField(8, P.vField(1, 3)))).button;
  assert.deepEqual(button, { code: 3, label: "double press" });
});

test("text fields come back as plain strings, never markup", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const info = [...P.lenField(3, str(evil))];
  const out = P.decodePendantMessage(msg(P.lenField(1, info)));
  assert.equal(typeof out.deviceInfo.firmware, "string");
  assert.equal(out.deviceInfo.firmware, evil); // the page renders this with textContent
});
