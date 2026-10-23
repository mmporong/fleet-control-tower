import { isLayout, isSnapshot } from './model.ts';
import type { Layout, Snapshot } from './types.ts';

export interface ReplayDocument {
  version: 1;
  playbackRate: 8;
  sampleInterval: 0.5;
  layout: Layout;
  frames: Snapshot[];
  source: {
    scenario: 'standard';
    seed: 7;
    strategy: 'locks+resolve';
    startTime: number;
    endTime: number;
  };
}

export function isReplayDocument(value: unknown): value is ReplayDocument {
  if (!value || typeof value !== 'object') return false;
  const replay = value as ReplayDocument;
  const source = replay.source;
  if (replay.version !== 1 || replay.playbackRate !== 8 || replay.sampleInterval !== 0.5
    || !isLayout(replay.layout) || !Array.isArray(replay.frames) || replay.frames.length === 0
    || !replay.frames.every(isSnapshot) || !source || source.scenario !== 'standard' || source.seed !== 7
    || source.strategy !== 'locks+resolve' || !Number.isFinite(source.startTime)
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

export function advanceReplayTime(
  currentTime: number,
  elapsedWallSeconds: number,
  playbackRate: number,
  startTime: number,
  endTime: number,
  paused: boolean,
): number {
  if (paused || elapsedWallSeconds <= 0) return currentTime;
  const duration = endTime - startTime;
  if (!Number.isFinite(duration) || duration <= 0) return startTime;
  const elapsedSimulation = elapsedWallSeconds * playbackRate;
  return startTime + (((currentTime - startTime + elapsedSimulation) % duration) + duration) % duration;
}
