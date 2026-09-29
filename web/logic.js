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
    ESTOP: { rank: 2, color: '#b42332', tone: 'danger', label: '비상정지' },
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

  function lerpAngle(a, b, t) {
    let d = ((b - a + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
    return a + d * t;
  }

  /** Nearest entity to a world point within a radius. items: [{id, x, y}] */
  function pick(items, x, y, radius) {
    let best = null;
    let bestD = radius;
    for (const it of items) {
      const d = Math.hypot(it.x - x, it.y - y);
      if (d <= bestD) {
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

  return { STATUS, linkInfo, statusInfo, exceptionSort, fitTransform, applyView, toScreen, toWorld, zoomAt, lerp, lerpAngle,
    pick, fmtDuration, fmtClock, fmtNum, alarmStateLabel, alarmLoad, sparkPath };
});
