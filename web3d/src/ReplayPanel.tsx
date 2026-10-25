import { matchingComparison, type ReplayIndex, type ReplayRecording, type ReplaySummary, type TimelineEvent } from './replay';

const RATE_OPTIONS = [0.5, 1, 2, 4, 8] as const;
const VARIANT_LABEL = {reference: '표준', before: '개선 전', after: '개선 후'} as const;

function formatTime(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
}

function formatDuration(seconds: number) {
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}시간`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}분`;
  return `${seconds}초`;
}

const EVENT_KIND_LABEL: Record<string, string> = {
  deadlock: '교착 감지', deadlock_resolved: '교착 해소', reroute: '경로 변경', evade: '비켜서기',
  yield: '양보', nudge: '이동 재개 유도', zone_speed: '구역 속도 조정', track_lost: '추적 종료',
  fault: '고장 발생', fault_cleared: '고장 해제', comms_lost: '통신 끊김',
  comms_restored: '통신 복구', order_rejected: '작업 거절', resync_ok: '작업 재개',
};

const SUMMARY_ROWS: {key: keyof ReplaySummary; label: string; unit: string; lower?: boolean}[] = [
  {key: 'putawayPerHour', label: '적치', unit: '건/h'},
  {key: 'shippedPerHour', label: '출고', unit: '건/h'},
  {key: 'totePerHour', label: '토트', unit: '건/h'},
  {key: 'queued', label: '대기 작업', unit: '건', lower: true},
  {key: 'contacts', label: '접촉', unit: '건', lower: true},
  {key: 'movingContacts', label: '이동 중 접촉', unit: '건', lower: true},
  {key: 'lockProblems', label: '잠금 문제', unit: '건', lower: true},
  {key: 'mismatch', label: '불일치', unit: '건', lower: true},
];

function Comparison({recordings, current}: {recordings: ReplayRecording[]; current: ReplayRecording | null}) {
  const pair = matchingComparison(recordings, current);
  if (!pair) return null;
  const {before, after} = pair;
  const rows = SUMMARY_ROWS.filter(row => before.summary?.[row.key] !== undefined && after.summary?.[row.key] !== undefined);
  if (!rows.length) return null;
  const duration = before.summary.durationSeconds;
  const warmup = before.summary.warmupSeconds;
  return <section className="comparison" aria-labelledby="comparison-title">
    <div><p className="eyebrow">SAME-CONDITION RESULT</p><h3 id="comparison-title">개선 전·후 전체 실험</h3><p>아래 수치는 짧은 재생 구간이 아니라 같은 조건으로 실행한 전체 실험 결과입니다.</p>{duration !== undefined ? <p className="measurement-scope">측정 {formatDuration(duration)}{warmup ? ` · 준비 구간 ${formatDuration(warmup)} 제외` : ''}</p> : null}</div>
    <div className="comparison-table" role="table" aria-label="동일 조건 전체 실험 결과">
      <div className="comparison-row comparison-head" role="row"><span role="columnheader">지표</span><span role="columnheader">개선 전</span><span role="columnheader">개선 후</span></div>
      {rows.map(row => {
        const beforeValue = before.summary?.[row.key] as number;
        const afterValue = after.summary?.[row.key] as number;
        const improved = row.lower ? afterValue < beforeValue : afterValue > beforeValue;
        return <div className="comparison-row" role="row" key={row.key}><span role="cell">{row.label}</span><span role="cell">{beforeValue.toLocaleString('ko-KR')} {row.unit}</span><strong role="cell" className={improved ? 'is-better' : ''}>{afterValue.toLocaleString('ko-KR')} {row.unit}</strong></div>;
      })}
    </div>
  </section>;
}

function RecordingTabs({index, selectedId, onSelect}: {index: ReplayIndex; selectedId: string; onSelect: (id: string) => void}) {
  return <div className="recording-tabs" role="group" aria-label="재생 기록 선택">
    {index.recordings.map(recording => <button key={recording.id} aria-pressed={recording.id === selectedId} onClick={() => onSelect(recording.id)}><span>{VARIANT_LABEL[recording.variant]}</span><strong>{recording.title}</strong></button>)}
  </div>;
}

export function ReplayLoadingPanel({index, selectedId, onSelect}: {index: ReplayIndex; selectedId: string; onSelect: (id: string) => void}) {
  const current = index.recordings.find(recording => recording.id === selectedId) ?? null;
  return <section className="replay-panel replay-loading" aria-busy="true" aria-labelledby="replay-loading-title">
    <div className="replay-heading"><div><p className="eyebrow">INCIDENT REPLAY</p><h2 id="replay-loading-title">{current?.title ?? '기록'} 불러오는 중</h2><p>다른 기록을 선택하면 현재 요청을 중단하고 새 기록을 읽습니다.</p></div><span className={`variant variant-${current?.variant ?? 'reference'}`}>{current ? VARIANT_LABEL[current.variant] : '기록'}</span></div>
    <RecordingTabs index={index} selectedId={selectedId} onSelect={onSelect} />
  </section>;
}

export function ReplayPanel({
  index, selectedId, onSelect, time, startTime, endTime, rate, onRate, onSeek, paused,
  onTogglePause, events, onPreviousEvent, onNextEvent, onEventSeek, placement,
}: {
  index: ReplayIndex; selectedId: string; onSelect: (id: string) => void;
  time: number; startTime: number; endTime: number; rate: number; onRate: (rate: number) => void;
  onSeek: (time: number) => void; paused: boolean; onTogglePause: () => void;
  events: TimelineEvent[]; onPreviousEvent: () => void; onNextEvent: () => void;
  onEventSeek: (time: number) => void;
  placement: 'controls' | 'details';
}) {
  const current = index.recordings.find(recording => recording.id === selectedId) ?? null;
  if (placement === 'details') return <section className="replay-panel replay-details" aria-labelledby="replay-analysis-title">
    <div className="replay-heading"><div><p className="eyebrow">CAUSE / RESULT</p><h2 id="replay-analysis-title">원인과 개선 결과</h2><p>사건을 선택하면 그 시점의 차량 위치와 대기 관계를 3D 화면에서 다시 확인할 수 있습니다.</p></div></div>
    <Comparison recordings={index.recordings} current={current} />
    <div className="event-list"><div className="event-list-heading"><h3>사건 타임라인</h3><span>같은 사건의 반복은 횟수와 마지막 시각까지 표시합니다.</span></div>{events.length ? <ol>{events.map((event, indexPosition) => <li key={`${event.firstTime}-${event.kind}-${event.source}-${indexPosition}`}><button onClick={() => onEventSeek(event.firstTime)}><time>{formatTime(event.firstTime)}</time><span className={`event-level level-${event.level.toLowerCase()}`}>{EVENT_KIND_LABEL[event.kind] ?? '기록'}</span><strong>{event.source}</strong><span>{event.msg}</span>{event.count > 1 ? <small>{event.count}회 · 마지막 {formatTime(event.lastTime)}</small> : null}</button></li>)}</ol> : <p className="empty-events">이 기록에는 별도로 남은 주요 사건이 없습니다.</p>}</div>
  </section>;
  return <section className="replay-panel" aria-labelledby="replay-title">
    <div className="replay-heading"><div><p className="eyebrow">INCIDENT REPLAY</p><h2 id="replay-title">정체 원인 재생</h2><p>{current?.description}</p></div><span className={`variant variant-${current?.variant ?? 'reference'}`}>{current ? VARIANT_LABEL[current.variant] : '기록'}</span></div>
    <RecordingTabs index={index} selectedId={selectedId} onSelect={onSelect} />
    <div className="transport">
      <button className="button transport-play" onClick={onTogglePause}>{paused ? '재생' : '멈춤'}</button>
      <label className="timeline-slider"><span><b>{formatTime(time)}</b><small>{formatTime(startTime)}–{formatTime(endTime)}</small></span><input aria-label="재생 시각" type="range" min={startTime} max={endTime} step="0.1" value={Math.min(endTime, Math.max(startTime, time))} onChange={event => onSeek(Number(event.target.value))} /></label>
      <div className="rate-buttons" role="group" aria-label="재생 속도">{RATE_OPTIONS.map(option => <button key={option} aria-pressed={rate === option} onClick={() => onRate(option)}>{option}×</button>)}</div>
      <div className="event-jump"><button onClick={onPreviousEvent} disabled={!events.length}>← 이전 주요 사건</button><button onClick={onNextEvent} disabled={!events.length}>다음 주요 사건 →</button></div>
    </div>
  </section>;
}
