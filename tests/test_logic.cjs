// node --test tests/test_logic.cjs
const test = require('node:test');
const assert = require('node:assert');
const L = require('../web/logic.js');

test('minimum zoom keeps forklift labels and leaders outside the full visible marker', () => {
  for (const scale of [3.05, 5.08, 17, 100]) {
    const r = L.forkliftMarkerRadius(scale) + 5;
    const obstacle = { x: 70-r, y: 70-r, w: 2*r, h: 2*r };
    const placed = L.placeMapLabels([{ id:'forklift', x:70, y:70, w:52, h:19 }], [obstacle], 180, 160);
    assert.ok(placed.length > 0);
    for (const p of placed) {
      assert.ok(p.box.x+p.box.w <= obstacle.x || p.box.x >= obstacle.x+obstacle.w || p.box.y+p.box.h <= obstacle.y || p.box.y >= obstacle.y+obstacle.h);
      assert.ok(Math.hypot(p.leader.from[0]-70,p.leader.from[1]-70) >= r);
    }
  }
});

test('visible marker edges remain selectable at minimum zoom and the nearest target wins', () => {
  const scale = 3.05, radius = L.forkliftMarkerRadius(scale);
  const forklift = { id:'forklift', x:0, y:0, hitRadius:(radius+4)/scale };
  assert.strictEqual(L.pick([forklift],(radius-1)/scale,0,1.2),forklift);
  assert.strictEqual(L.pick([forklift],(radius+5)/scale,0,1.2),null);
  const near = { id:'near', x:1, y:0 };
  assert.strictEqual(L.pick([forklift,near],1.1,0,1.2),near);
});

test('stationary forklift motion does not turn sensor velocity noise into a travel direction', () => {
  for (const heading of [-Math.PI, -1.3, 0, 2.8]) {
    const motion = L.trackMotion({ stopped: true, heading, speed: 0.9 });
    assert.deepStrictEqual(motion, { speed: 0, heading: null, vx: 0, vy: 0 });
  }
});

test('moving forklift travel direction uses displacement heading and pace', () => {
  const motion = L.trackMotion({ stopped: false, heading: Math.PI / 2, speed: 1.2 });
  assert.strictEqual(motion.speed, 1.2);
  assert.strictEqual(motion.heading, Math.PI / 2);
  assert.ok(Math.abs(motion.vx) < 1e-9);
  assert.ok(Math.abs(motion.vy - 1.2) < 1e-9);
});

test('missing or unconfirmed forklift direction stays unknown instead of using raw velocity', () => {
  assert.deepStrictEqual(L.trackMotion(), { speed: null, heading: null, vx: 0, vy: 0 });
  assert.deepStrictEqual(L.trackMotion({ stopped: false, heading: null, speed: 0.8 }),
    { speed: 0.8, heading: null, vx: 0, vy: 0 });
});

test('invalid forklift motion values cannot produce a direction line', () => {
  for (const meta of [{ stopped: false, heading: 0, speed: NaN },
    { stopped: false, heading: Infinity, speed: 1 }, { stopped: false, heading: 0, speed: -1 }]) {
    const motion = L.trackMotion(meta);
    assert.strictEqual(motion.heading, null);
    assert.strictEqual(motion.vx, 0);
    assert.strictEqual(motion.vy, 0);
  }
});

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
