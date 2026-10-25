import type { Layout, Robot, Snapshot } from './types.ts';

function optionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'string';
}

function stringRecord(value: unknown): boolean {
  return !!value && typeof value === 'object' && !Array.isArray(value)
    && Object.values(value).every(item => typeof item === 'string');
}

export function isSnapshot(value: unknown): value is Snapshot {
  if (!value || typeof value !== 'object') return false;
  const s = value as Snapshot;
  return Number.isFinite(s.t) && typeof s.layout_key === 'string' && typeof s.scenario === 'string'
    && Array.isArray(s.robots) && s.robots.every(r => [r.id, r.cls, r.st, r.label, r.conn, r.purpose].every(item => typeof item === 'string')
      && [r.x, r.y, r.th, r.v, r.len, r.wid, r.bat].every(Number.isFinite)
      && typeof r.load === 'boolean' && typeof r.positioned === 'boolean'
      && optionalString(r.goal) && Array.isArray(r.base) && r.base.every(item => typeof item === 'string')
      && Array.isArray(r.route) && r.route.every(item => typeof item === 'string')
      && [r.why, r.wait_kind, r.wait_holder, r.wait_resource, r.task, r.stop].every(optionalString)
      && (r.wait_s === undefined || Number.isFinite(r.wait_s))) && !!s.kpi && !!s.alarm_counts
    && (s.locks === undefined || stringRecord(s.locks))
    && (s.events === undefined || (Array.isArray(s.events) && s.events.every(event => Number.isFinite(event.t)
      && ['kind', 'source', 'msg', 'level'].every(key => typeof event[key as keyof typeof event] === 'string'))));
}
export function isLayout(value: unknown): value is Layout {
  if (!value || typeof value !== 'object') return false;
  const l = value as Layout;
  return Number.isFinite(l.width) && l.width > 0 && Number.isFinite(l.height) && l.height > 0
    && Array.isArray(l.nodes) && l.nodes.every(n => typeof n.id === 'string' && [n.x,n.y].every(Number.isFinite))
    && Array.isArray(l.edges) && Array.isArray(l.racks) && Array.isArray(l.zones);
}
export function inventoryTotal(s: Snapshot): number {
  return Object.values(s.site_state?.rack ?? {}).reduce((sum, count) => sum + count, 0);
}
export function selectedEntity(s: Snapshot, id: string | null) {
  const robot = s.robots.find(r => r.id === id);
  if (robot) return { id: robot.id, kind: robot.cls === 'FORKLIFT' ? '자율 지게차' : '토트 AMR',
    x: robot.x, y: robot.y, speed: robot.v, label: robot.label, robot };
  const f = s.truth?.forklifts.find(f => f[0] === id);
  if (f) return {id: f[0], kind: '수동 지게차', x: f[1], y: f[2], speed: f[4], label: '시뮬레이터 위치·방향', robot: null};
  const h = s.truth?.humans.find(h => h[0] === id);
  if (h) return {id: h[0], kind: '작업자', x: h[1], y: h[2], speed: h[5], label: '시뮬레이터 위치', robot: null};
  return null;
}
export function robotStatusClass(r: Robot) {
  if (r.conn !== 'ONLINE') return 'offline';
  if (['FAULT', 'ESTOP', 'PROTECTIVE_STOP', 'BLOCKED'].includes(r.st)) return 'attention';
  return r.v > 0 ? 'moving' : 'idle';
}
