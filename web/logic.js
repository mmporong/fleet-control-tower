/* Pure helpers for the control room UI. Loaded in the browser as window.FleetLogic
   and in Node for tests (module.exports). No DOM access here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FleetLogic = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ISA-101 style: normal states stay neutral grey, colour is reserved for what needs attention.
  const STATUS = {
    FAULT: { rank: 0, color: '#b42332', tone: 'danger', label: '고장' },
    OFFLINE: { rank: 1, color: '#b42332', tone: 'danger', label: '통신 두절' },
    ESTOP: { rank: 2, color: '#b42332', tone: 'danger', label: '일괄 정지' },
    PROTECTIVE: { rank: 3, color: '#c2410c', tone: 'warn', label: '보호정지' },
    MANUAL: { rank: 4, color: '#6d28d9', tone: 'special', label: '원격 조종' },
    BLOCKED: { rank: 5, color: '#b45309', tone: 'warn', label: '차단 대기' },
    WAITING: { rank: 6, color: '#a16207', tone: 'warn', label: '교통 대기' },
    PAUSED: { rank: 7, color: '#64748b', tone: 'muted', label: '일시정지' },
    ACTION: { rank: 8, color: '#334155', tone: 'normal', label: '작업 동작' },
    MOVING: { rank: 9, color: '#334155', tone: 'normal', label: '주행' },
    CHARGING: { rank: 10, color: '#94a3b8', tone: 'muted', label: '충전' },
    IDLE: { rank: 11, color: '#94a3b8', tone: 'muted', label: '대기' },
    PARKED: { rank: 12, color: '#cbd5e1', tone: 'muted', label: '주차' },
  };

  function statusInfo(st) {
    return STATUS[st] || { rank: 20, color: '#94a3b8', tone: 'muted', label: st || '—' };
  }

  // Displacement-based travel direction is not the forklift's body orientation.
  function trackMotion(meta) {
    const speed = meta?.stopped === true ? 0 : meta?.stopped === false && Number.isFinite(meta.speed) && meta.speed >= 0 ? meta.speed : null;
    const heading = speed > 0 && Number.isFinite(meta?.heading) ? meta.heading : null;
    return { speed, heading, vx: heading == null ? 0 : speed * Math.cos(heading), vy: heading == null ? 0 : speed * Math.sin(heading) };
  }

  function forkliftMarkerRadius(scale) { return Math.max(10, Math.min(22, 1.3 * scale)); }

  /** Exception-first ordering: abnormal units on top, then longest wait, then id. */
  function exceptionSort(robots) {
    return robots.slice().sort((a, b) => {
      const ra = statusInfo(a.st).rank;
      const rb = statusInfo(b.st).rank;
      if (ra !== rb) return ra - rb;
      if ((b.wait_s || 0) !== (a.wait_s || 0)) return (b.wait_s || 0) - (a.wait_s || 0);
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
  }

  /** Fit a world rectangle (metres, y up) into a canvas (pixels, y down) with a margin. */
  function fitTransform(worldW, worldH, canvasW, canvasH, margin) {
    const m = margin == null ? 12 : margin;
    const scale = Math.max(0.01, Math.min((canvasW - 2 * m) / worldW, (canvasH - 2 * m) / worldH));
    const ox = (canvasW - worldW * scale) / 2;
    const oy = (canvasH - worldH * scale) / 2;
    return { scale, ox, oy, worldH };
  }

  function applyView(base, view) {
    // view: {zoom, panX, panY} in pixels, zoom about the canvas origin after fitting
    const z = view && view.zoom ? view.zoom : 1;
    return { scale: base.scale * z, ox: base.ox * z + (view ? view.panX : 0), oy: base.oy * z + (view ? view.panY : 0),
      worldH: base.worldH };
  }

  function toScreen(t, x, y) {
    return [t.ox + x * t.scale, t.oy + (t.worldH - y) * t.scale];
  }

  function toWorld(t, px, py) {
    return [(px - t.ox) / t.scale, t.worldH - (py - t.oy) / t.scale];
  }

  /** Zoom keeping the world point under the cursor fixed. */
  function zoomAt(view, base, px, py, factor) {
    const before = applyView(base, view);
    const w = toWorld(before, px, py);
    const zoom = Math.max(0.6, Math.min(6, (view.zoom || 1) * factor));
    const next = { zoom, panX: 0, panY: 0 };
    const t = applyView(base, next);
    const [sx, sy] = toScreen(t, w[0], w[1]);
    next.panX = px - sx;
    next.panY = py - sy;
    return next;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  /** Place annotations in free screen space; omit a label when no readable position fits. */
  function placeMapLabels(labels, obstacles, width, height) {
    const occupied = obstacles.slice();
    const placed = [];
    const leaders = [];
    const overlaps = (a, b) => a.x < b.x + b.w + 3 && a.x + a.w + 3 > b.x && a.y < b.y + b.h + 3 && a.y + a.h + 3 > b.y;
    const contains = (r, x, y) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
    const hitsRect = (line, r) => {
      let low = 0, high = 1;
      for (const [a, b, min, max] of [[line.from[0], line.to[0], r.x, r.x + r.w], [line.from[1], line.to[1], r.y, r.y + r.h]]) {
        const delta = b - a;
        if (Math.abs(delta) < 1e-9) { if (a < min || a > max) return false; }
        else {
          const first = (min - a) / delta, last = (max - a) / delta;
          low = Math.max(low, Math.min(first, last)); high = Math.min(high, Math.max(first, last));
          if (low > high) return false;
        }
      }
      return true;
    };
    const crosses = (a, b) => {
      const dx = a.to[0] - a.from[0], dy = a.to[1] - a.from[1];
      const ex = b.to[0] - b.from[0], ey = b.to[1] - b.from[1];
      const ox = b.from[0] - a.from[0], oy = b.from[1] - a.from[1], det = dx * ey - dy * ex;
      if (Math.abs(det) < 1e-9) {
        if (Math.abs(ox * dy - oy * dx) > 1e-9) return false;
        return Math.max(Math.min(a.from[0], a.to[0]), Math.min(b.from[0], b.to[0])) <= Math.min(Math.max(a.from[0], a.to[0]), Math.max(b.from[0], b.to[0])) &&
          Math.max(Math.min(a.from[1], a.to[1]), Math.min(b.from[1], b.to[1])) <= Math.min(Math.max(a.from[1], a.to[1]), Math.max(b.from[1], b.to[1]));
      }
      const t = (ox * ey - oy * ex) / det, u = (ox * dy - oy * dx) / det;
      return t >= 0 && t <= 1 && u >= 0 && u <= 1;
    };
    for (const label of labels.slice().sort((a, b) => (b.priority || 0) - (a.priority || 0) || a.id.localeCompare(b.id))) {
      if (label.x < 0 || label.x > width || label.y < 0 || label.y > height) continue;
      const w = label.w, h = label.h;
      const gap = label.gap || 10;
      const candidates = [[gap, -h - gap], [-w - gap, -h - gap], [gap, gap], [-w - gap, gap],
        [-w / 2, -h - gap - 18], [-w / 2, gap + 18], [gap + 22, -h / 2], [-w - gap - 22, -h / 2]];
      for (const [dx, dy] of candidates) {
        const box = { x: label.x + dx, y: label.y + dy, w, h };
        if (box.x < 6 || box.y < 6 || box.x + w > width - 6 || box.y + h > height - 6 || occupied.some(r => overlaps(box, r)) || leaders.some(line => hitsRect(line, box))) continue;
        const to = [Math.max(box.x, Math.min(label.x, box.x + w)), Math.max(box.y, Math.min(label.y, box.y + h))];
        const vx = to[0] - label.x, vy = to[1] - label.y, distance = Math.hypot(vx, vy);
        let exit = 0;
        for (const r of obstacles.filter(r => contains(r, label.x, label.y))) {
          const tx = vx > 0 ? (r.x + r.w - label.x) / vx : vx < 0 ? (r.x - label.x) / vx : Infinity;
          const ty = vy > 0 ? (r.y + r.h - label.y) / vy : vy < 0 ? (r.y - label.y) / vy : Infinity;
          exit = Math.max(exit, Math.min(tx, ty));
        }
        const start = Math.min(1, exit + 2 / Math.max(1, distance));
        const leader = { from: [label.x + vx * start, label.y + vy * start], to };
        if (occupied.some(r => hitsRect(leader, r)) || leaders.some(line => crosses(leader, line))) continue;
        occupied.push(box);
        leaders.push(leader);
        placed.push({ ...label, box, leader });
        break;
      }
    }
    return placed;
  }

  function lerpAngle(a, b, t) {
    let d = ((b - a + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
    return a + d * t;
  }

  /** Nearest entity to a world point within a radius. items: [{id, x, y}] */
  function pick(items, x, y, radius) {
    let best = null;
    let bestD = Infinity;
    for (const it of items) {
      const d = Math.hypot(it.x - x, it.y - y);
      if (d <= (it.hitRadius ?? radius) && d <= bestD) {
        best = it;
        bestD = d;
      }
    }
    return best;
  }

  function fmtDuration(sec) {
    if (sec == null || !isFinite(sec)) return '—';
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return `${s}초`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}분 ${s % 60}초`;
    return `${Math.floor(m / 60)}시간 ${m % 60}분`;
  }

  function fmtClock(simSeconds) {
    const s = Math.floor(simSeconds || 0);
    const h = String(Math.floor(s / 3600) + 8).padStart(2, '0');   // shift starts at 08:00
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const r = String(s % 60).padStart(2, '0');
    return `${h}:${m}:${r}`;
  }

  function fmtNum(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    return Number(v).toFixed(digits == null ? 0 : digits);
  }

  const ALARM_STATE = { UNACK: '미확인', ACKED: '확인', RTN_UNACK: '정상복귀·미확인', SHELVED: '보류' };

  function alarmStateLabel(state) {
    return ALARM_STATE[state] || state;
  }

  /** Alarm-count health vs ISA-18.2 guidance (~1 per 10 min manageable, >10 per 10 min is a flood). */
  function alarmLoad(last10min) {
    if (last10min > 10) return { tone: 'danger', label: '알람 홍수' };
    if (last10min > 2) return { tone: 'warn', label: '부하 높음' };
    return { tone: 'ok', label: '관리 가능' };
  }

  /** SVG path for a sparkline of numbers in a w x h box. */
  // Link quality as the control room measures it: share of expected state/visualization
  // messages received in the last 5 s and median one-way delay.
  function linkInfo(conn, share, delayMs) {
    if (conn !== 'ONLINE') return { tone: 'danger', text: conn === 'CONNECTIONBROKEN' ? '두절' : conn || '미연결' };
    if (share == null) return { tone: 'ok', text: '정상' };
    const delay = delayMs == null ? '' : ` · ${delayMs} ms`;
    const tone = share < 60 || delayMs > 1000 ? 'danger' : share < 85 || delayMs > 400 ? 'warn' : 'ok';
    return { tone, text: `수신 ${share}%${delay}` };
  }

  function sparkPath(values, w, h) {
    const vals = values.filter((v) => v != null && isFinite(v));
    if (vals.length < 2) return '';
    const max = Math.max(...vals);
    const min = Math.min(0, ...vals);
    const span = max - min || 1;
    return vals.map((v, i) => `${i ? 'L' : 'M'}${((i / (vals.length - 1)) * w).toFixed(1)},${(h - ((v - min) / span) * h).toFixed(1)}`).join(' ');
  }

  return { STATUS, linkInfo, statusInfo, trackMotion, forkliftMarkerRadius, exceptionSort, fitTransform, applyView, toScreen, toWorld, zoomAt, lerp, lerpAngle, placeMapLabels,
    pick, fmtDuration, fmtClock, fmtNum, alarmStateLabel, alarmLoad, sparkPath };
});
