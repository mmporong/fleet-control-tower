import { isLayout, isSnapshot } from './model.ts';
import type { IncidentEvent, Layout, Snapshot } from './types.ts';

export type ReplayVariant = 'reference' | 'before' | 'after';
export interface ReplaySummary {
  durationSeconds?: number; warmupSeconds?: number; putawayPerHour?: number; shippedPerHour?: number;
  totePerHour?: number; queued?: number; contacts?: number; movingContacts?: number;
  lockProblems?: number; mismatch?: number;
}
export interface ReplayRecording {
  id: string; title: string; description: string; url: string; variant: ReplayVariant;
  group?: string; summary?: ReplaySummary;
}
export interface ReplayIndex { version: 1; recordings: ReplayRecording[] }

export interface ReplayDocument {
  version: 1;
  playbackRate: number;
  sampleInterval: number;
  layout: Layout;
  frames: Snapshot[];
  source: {
    scenario: string;
    seed: number;
    strategy: 'none' | 'locks' | 'locks+resolve';
    startTime: number;
    endTime: number;
    commit?: string;
  };
}

const STRATEGIES = new Set(['none', 'locks', 'locks+resolve']);
const VARIANTS = new Set<ReplayVariant>(['reference', 'before', 'after']);

export function isSafeReplayUrl(url: string): boolean {
  if (!url || url.startsWith('//') || /^[a-z][a-z\d+.-]*:/i.test(url)) return false;
  try {
    const parsed = new URL(url, 'https://fleet.invalid/view/');
    return parsed.origin === 'https://fleet.invalid';
  } catch { return false; }
}

function isReplaySummary(value: unknown): value is ReplaySummary {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  return Object.values(value).every(item => typeof item === 'number' && Number.isFinite(item));
}

export function isReplayIndex(value: unknown): value is ReplayIndex {
  if (!value || typeof value !== 'object') return false;
  const index = value as ReplayIndex;
  if (index.version !== 1 || !Array.isArray(index.recordings) || index.recordings.length === 0) return false;
  const ids = new Set<string>();
  return index.recordings.every(recording => {
    if (!recording || typeof recording !== 'object' || typeof recording.id !== 'string' || !recording.id
      || ids.has(recording.id) || typeof recording.title !== 'string' || !recording.title
      || typeof recording.description !== 'string' || typeof recording.url !== 'string'
      || !isSafeReplayUrl(recording.url) || !VARIANTS.has(recording.variant)
      || (recording.group !== undefined && typeof recording.group !== 'string')
      || (recording.summary !== undefined && !isReplaySummary(recording.summary))) return false;
    ids.add(recording.id);
    return true;
  });
}

export async function loadReplayIndex(
  baseUrl: string,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<ReplayIndex> {
  const response = await fetcher(`${baseUrl}replay-index.json`, {signal, credentials: 'same-origin'});
  if (response.status === 404) return {version: 1, recordings: [{
    id: 'reference', title: '표준 운영', description: '기존 시뮬레이션 기록',
    url: 'replay.json', variant: 'reference',
  }]};
  if (!response.ok) throw Error('기록 목록을 불러오지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.');
  let value: unknown;
  try { value = await response.json(); }
  catch { throw Error('기록 목록의 JSON 형식을 확인해 주세요.'); }
  if (!isReplayIndex(value)) throw Error('기록 목록의 형식이나 경로를 확인해 주세요.');
  return value;
}

export function recordingById(index: ReplayIndex, id: string): ReplayRecording | null {
  return index.recordings.find(recording => recording.id === id) ?? null;
}

export function matchingComparison(
  recordings: ReplayRecording[],
  current: ReplayRecording | null,
): {before: ReplayRecording & {summary: ReplaySummary}; after: ReplayRecording & {summary: ReplaySummary}} | null {
  const candidateGroups = current?.group
    ? [current.group]
    : [...new Set(recordings.filter(item => item.variant === 'before' && item.group).map(item => item.group as string))];
  for (const group of candidateGroups) {
    const before = recordings.find(item => item.variant === 'before' && item.group === group);
    const after = recordings.find(item => item.variant === 'after' && item.group === group);
    if (!before?.summary || !after?.summary) continue;
    if (before.summary.durationSeconds === undefined || before.summary.warmupSeconds === undefined
      || before.summary.durationSeconds !== after.summary.durationSeconds
      || before.summary.warmupSeconds !== after.summary.warmupSeconds) continue;
    return {before: before as ReplayRecording & {summary: ReplaySummary}, after: after as ReplayRecording & {summary: ReplaySummary}};
  }
  return null;
}

export async function loadReplayDocument(
  baseUrl: string,
  recording: ReplayRecording,
  signal: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<ReplayDocument> {
  if (!isSafeReplayUrl(recording.url)) throw Error('기록 경로는 같은 사이트 안의 상대 경로만 사용할 수 있습니다.');
  const response = await fetcher(new URL(recording.url, baseUrl), {signal, credentials: 'same-origin'});
  if (!response.ok) throw Error('저장된 시뮬레이션 기록을 불러오지 못했습니다.');
  let value: unknown;
  try { value = await response.json(); }
  catch { throw Error('저장된 시뮬레이션 기록의 JSON 형식을 확인해 주세요.'); }
  if (!isReplayDocument(value)) throw Error('저장된 시뮬레이션 기록의 형식을 확인해 주세요.');
  return value;
}

export function isReplayDocument(value: unknown): value is ReplayDocument {
  if (!value || typeof value !== 'object') return false;
  const replay = value as ReplayDocument;
  const source = replay.source;
  if (replay.version !== 1 || !Number.isFinite(replay.playbackRate) || replay.playbackRate <= 0
    || !Number.isFinite(replay.sampleInterval) || replay.sampleInterval <= 0
    || !isLayout(replay.layout) || !Array.isArray(replay.frames) || replay.frames.length === 0
    || !replay.frames.every(isSnapshot) || !source || typeof source.scenario !== 'string'
    || !Number.isInteger(source.seed) || !STRATEGIES.has(source.strategy)
    || typeof source.commit !== 'string' || !source.commit || !Number.isFinite(source.startTime)
    || !Number.isFinite(source.endTime) || source.endTime <= source.startTime) return false;
  return replay.frames.every((frame, index) => frame.scenario === source.scenario
    && !!frame.site_state && typeof frame.site_state === 'object'
    && [frame.kpi.working, frame.kpi.queued, frame.kpi.done,
      frame.alarm_counts.p1, frame.alarm_counts.p2, frame.alarm_counts.p3].every(Number.isFinite)
    && frame.t >= source.startTime && frame.t <= source.endTime
    && (index === 0 || frame.t > replay.frames[index - 1].t));
}

export function frameAtTime(frames: Snapshot[], simulationTime: number): Snapshot {
  let low = 0;
  let high = frames.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (frames[middle].t <= simulationTime) low = middle + 1;
    else high = middle - 1;
  }
  return frames[Math.max(0, high)];
}

export function frameAtOrAfterTime(frames: Snapshot[], simulationTime: number): Snapshot {
  let low = 0;
  let high = frames.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (frames[middle].t < simulationTime) low = middle + 1;
    else high = middle - 1;
  }
  return frames[Math.min(frames.length - 1, low)];
}

export interface ReplayClockState { time: number; carry: number }

export function advanceReplayClock(
  current: ReplayClockState,
  elapsedWallSeconds: number,
  playbackRate: number,
  startTime: number,
  endTime: number,
  paused: boolean,
): ReplayClockState {
  if (paused || elapsedWallSeconds <= 0) return current;
  const duration = endTime - startTime;
  if (!Number.isFinite(duration) || duration <= 0) return {time: startTime, carry: 0};
  const elapsedSimulation = elapsedWallSeconds * playbackRate;
  if (current.time === endTime) {
    const phase = (current.carry + elapsedSimulation) % duration;
    return {time: startTime + phase, carry: 0};
  }
  const next = current.time + elapsedSimulation;
  if (current.time < endTime && next >= endTime) {
    return {time: endTime, carry: (next - endTime) % duration};
  }
  return {time: startTime + (((next - startTime) % duration) + duration) % duration, carry: 0};
}

export interface TimelineEvent extends IncidentEvent {
  firstTime: number; lastTime: number; count: number;
}

export function timelineEvents(frames: Snapshot[]): TimelineEvent[] {
  const startTime = frames[0]?.t ?? 0;
  const endTime = frames.at(-1)?.t ?? startTime;
  const unique = new Map<string, IncidentEvent>();
  frames.forEach(frame => (frame.events ?? []).forEach(raw => {
    const event: IncidentEvent = {...raw, kind: 'kind' in raw ? raw.kind : raw.type};
    if (!Number.isFinite(event.t) || event.t < startTime || event.t > endTime
      || !event.kind || !event.source || !event.msg || !event.level) return;
    unique.set(`${event.t}\u0000${event.kind}\u0000${event.source}\u0000${event.msg}\u0000${event.level}`, event);
  }));
  const sorted = [...unique.values()].sort((a, b) => a.t - b.t);
  const folded: TimelineEvent[] = [];
  sorted.forEach(event => {
    const previous = folded.at(-1);
    if (previous && previous.kind === event.kind && previous.source === event.source
      && previous.msg === event.msg && previous.level === event.level && event.t - previous.lastTime <= 10) {
      previous.lastTime = event.t;
      previous.count += 1;
    } else folded.push({...event, firstTime: event.t, lastTime: event.t, count: 1});
  });
  return folded;
}

export function isMajorEvent(event: IncidentEvent): boolean {
  return ['warn', 'warning', 'error', 'crit', 'critical', 'p1', 'p2'].includes(event.level.toLowerCase())
    || /(block|deadlock|yield|reroute|resolve|fault|stop)/i.test(event.kind);
}

export function adjacentMajorEvent(events: TimelineEvent[], time: number, direction: -1 | 1): TimelineEvent | null {
  const major = events.filter(isMajorEvent);
  if (direction < 0) return [...major].reverse().find(event => event.firstTime < time - 0.001) ?? null;
  return major.find(event => event.firstTime > time + 0.001) ?? null;
}
