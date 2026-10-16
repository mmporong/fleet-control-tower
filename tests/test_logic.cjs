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

test('link quality tone follows measured share and delay', () => {
  assert.strictEqual(L.linkInfo('ONLINE', 98, 100).tone, 'ok');
  assert.strictEqual(L.linkInfo('ONLINE', 70, 100).tone, 'warn');
  assert.strictEqual(L.linkInfo('ONLINE', 98, 700).tone, 'warn');
  assert.strictEqual(L.linkInfo('ONLINE', 40, 100).tone, 'danger');
  assert.deepStrictEqual(L.linkInfo('CONNECTIONBROKEN', 0, null), { tone: 'danger', text: '두절' });
  assert.strictEqual(L.linkInfo('ONLINE', null, null).text, '정상');
});

test('crowded map labels stay within the canvas and clear of units and each other', () => {
  const obstacles = [{ x: 110, y: 75, w: 50, h: 50 }, { x: 190, y: 60, w: 35, h: 70 }];
  const labels = Array.from({ length: 12 }, (_, i) => ({ id: `unit-${i}`, x: 135 + i * 4, y: 100, w: 64, h: 19 }));
  const result = L.placeMapLabels(labels, obstacles, 360, 220);
  assert.ok(result.length > 0 && result.length < labels.length);
  const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  result.forEach(({ box }, i) => {
    assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.w <= 360 && box.y + box.h <= 220);
    assert.ok(!obstacles.concat(result.slice(0, i).map(r => r.box)).some(b => overlaps(box, b)));
  });
});

test('selected map label gets the remaining space before a routine label', () => {
  const labels = [{ id: 'routine', x: 50, y: 50, w: 45, h: 19 },
    { id: 'selected', x: 50, y: 50, w: 45, h: 19, priority: 100 }];
  const result = L.placeMapLabels(labels, [], 120, 80);
  assert.strictEqual(result[0].id, 'selected');
});

test('map annotations omit text when no free position fits', () => {
  assert.deepStrictEqual(L.placeMapLabels([{ id: 'blocked', x: 50, y: 50, w: 80, h: 20 }],
    [{ x: 0, y: 0, w: 100, h: 100 }], 100, 100), []);
});

test('map label placement is stable and preserves its inputs', () => {
  const labels = [{ id: 'B', x: 100, y: 100, w: 50, h: 19 }, { id: 'A', x: 100, y: 100, w: 50, h: 19 }];
  const obstacles = [{ x: 90, y: 90, w: 20, h: 20 }];
  const before = JSON.stringify({ labels, obstacles });
  assert.deepStrictEqual(L.placeMapLabels(labels, obstacles, 300, 220), L.placeMapLabels([...labels].reverse(), obstacles, 300, 220));
  assert.strictEqual(JSON.stringify({ labels, obstacles }), before);
});

test('annotation leaders start outside vehicle bodies and avoid other text', () => {
  const units = [{ x: 85, y: 85, w: 30, h: 30 }, { x: 190, y: 110, w: 30, h: 30 }];
  const placed = L.placeMapLabels([{ id: 'A', x: 100, y: 100, w: 55, h: 19 },
    { id: 'B', x: 205, y: 125, w: 55, h: 19 }], units, 360, 240);
  assert.strictEqual(placed.length, 2);
  const inside = (r, x, y) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
  for (const label of placed) {
    for (let step = 0; step <= 100; step++) {
      const t = step / 100, x = label.leader.from[0] * (1 - t) + label.leader.to[0] * t;
      const y = label.leader.from[1] * (1 - t) + label.leader.to[1] * t;
      assert.ok(!units.concat(placed.filter(p => p !== label).map(p => p.box)).some(r => inside(r, x, y)));
    }
  }
});

test('offscreen units do not receive misleading labels at the viewport edge', () => {
  assert.deepStrictEqual(L.placeMapLabels([{ id: 'outside', x: -10, y: 80, w: 60, h: 19 }], [], 300, 200), []);
});
