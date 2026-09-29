// node --test tests/test_logic.cjs
const test = require('node:test');
const assert = require('node:assert');
const L = require('../web/logic.js');

test('exception-first ordering puts faults and long waits on top', () => {
  const rows = L.exceptionSort([
    { id: 'A', st: 'MOVING', wait_s: 0 }, { id: 'B', st: 'WAITING', wait_s: 5 },
    { id: 'C', st: 'FAULT', wait_s: 0 }, { id: 'D', st: 'WAITING', wait_s: 40 }, { id: 'E', st: 'PARKED' },
  ]);
  assert.deepStrictEqual(rows.map((r) => r.id), ['C', 'D', 'B', 'A', 'E']);
});

test('world/screen transforms round-trip and y flips', () => {
  const t = L.fitTransform(60, 34, 1200, 700, 10);
  const [px, py] = L.toScreen(t, 10, 5);
  const [x, y] = L.toWorld(t, px, py);
  assert.ok(Math.abs(x - 10) < 1e-9 && Math.abs(y - 5) < 1e-9);
  assert.ok(L.toScreen(t, 0, 34)[1] < L.toScreen(t, 0, 0)[1]);
});

test('zoom keeps the point under the cursor', () => {
  const base = L.fitTransform(60, 34, 1200, 700, 10);
  const view = { zoom: 1, panX: 0, panY: 0 };
  const before = L.toWorld(L.applyView(base, view), 400, 300);
  const next = L.zoomAt(view, base, 400, 300, 2);
  const after = L.toWorld(L.applyView(base, next), 400, 300);
  assert.ok(Math.abs(before[0] - after[0]) < 1e-6 && Math.abs(before[1] - after[1]) < 1e-6);
});

test('angle interpolation takes the short way round', () => {
  assert.ok(Math.abs(L.lerpAngle(3.0, -3.0, 0.5) - Math.PI) < 0.2 || Math.abs(L.lerpAngle(3.0, -3.0, 0.5) + Math.PI) < 0.2);
});

test('alarm load follows the ISA-18.2 thresholds', () => {
  assert.strictEqual(L.alarmLoad(1).tone, 'ok');
  assert.strictEqual(L.alarmLoad(5).tone, 'warn');
  assert.strictEqual(L.alarmLoad(11).label, '알람 홍수');
});

test('formatting helpers', () => {
  assert.strictEqual(L.fmtDuration(75), '1분 15초');
  assert.strictEqual(L.fmtClock(3661), '09:01:01');
  assert.strictEqual(L.sparkPath([1], 100, 10), '');
  assert.ok(L.sparkPath([0, 1, 2], 100, 10).startsWith('M0.0,10.0'));
});
