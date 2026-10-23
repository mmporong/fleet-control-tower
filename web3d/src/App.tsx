import { Component, Suspense, lazy, useEffect, useRef, useState, type ReactNode } from 'react';
import type { Layout, SceneProps, Snapshot } from './types';
import { inventoryTotal, isLayout, isSnapshot, robotStatusClass, selectedEntity } from './model';
import { advanceReplayTime, frameAtTime, isReplayDocument } from './replay';
const WarehouseScene = lazy(() => import('./WarehouseScene'));
const REPLAY_MODE = import.meta.env.VITE_FLEET_REPLAY === 'true';
const PORTFOLIO_URL = new URL('../#more-projects', location.href).href;

class SceneBoundary extends Component<{children: ReactNode; fallbackHref: string}, {failed: boolean}> {
  state = {failed: false};
  static getDerivedStateFromError() { return {failed: true}; }
  render() { return this.state.failed ? <div className="scene-message" role="alert"><strong>3D 화면을 표시할 수 없습니다.</strong><p>브라우저의 그래픽 지원을 확인해 주세요.</p><a href={this.props.fallbackHref}>{REPLAY_MODE ? '포트폴리오로 돌아가기' : '2D 관제로 이동'}</a></div> : this.props.children; }
}
function useReducedMotion() {
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => { const query = matchMedia('(prefers-reduced-motion: reduce)'); const update = () => setReduced(query.matches); query.addEventListener('change', update); return () => query.removeEventListener('change', update); }, []);
  return reduced;
}
async function getJson(path: string, signal: AbortSignal) {
  const response = await fetch(path, {signal, credentials: 'same-origin'});
  if (response.status === 401) throw Error('로그인이 필요합니다. 2D 관제에서 로그인한 뒤 다시 열어 주세요.');
  if (!response.ok) throw Error('관제 데이터를 받지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.');
  return response.json();
}
export function App() {
  const reducedMotion = useReducedMotion();
  const [layout, setLayout] = useState<Layout | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const [stale, setStale] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [replayRate, setReplayRate] = useState(8);
  const [paused, setPaused] = useState(reducedMotion);
  const pausedRef = useRef(paused);
  const latest = useRef<Snapshot | null>(null);
  const receivedAt = useRef(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [cameraView, setCameraView] = useState<SceneProps['cameraView']>('overview');
  const [cameraRevision, setCameraRevision] = useState(0);
  const [showRoutes, setShowRoutes] = useState(true);
  const [showRacks, setShowRacks] = useState(true);
  useEffect(() => {
    const abort = new AbortController();
    setError(''); setConnected(false); setSnapshot(null); setLayout(null); latest.current = null;
    if (REPLAY_MODE) {
      let timer: ReturnType<typeof setInterval> | undefined;
      const startReplay = async () => {
        try {
          const response = await fetch(`${import.meta.env.BASE_URL}replay.json`, {signal: abort.signal, credentials: 'same-origin'});
          if (!response.ok) throw Error('저장된 시뮬레이션 기록을 불러오지 못했습니다.');
          const replay: unknown = await response.json();
          if (!isReplayDocument(replay)) throw Error('저장된 시뮬레이션 기록의 형식을 확인해 주세요.');
          if (abort.signal.aborted) return;
          let playbackTime = replay.source.startTime;
          let previousWallTime = performance.now();
          const initialFrame = frameAtTime(replay.frames, playbackTime);
          latest.current = initialFrame;
          setReplayRate(replay.playbackRate); setLayout(replay.layout); setSnapshot(initialFrame); setConnected(true);
          timer = setInterval(() => {
            const wallTime = performance.now();
            const elapsedSeconds = (wallTime - previousWallTime) / 1000;
            previousWallTime = wallTime;
            playbackTime = advanceReplayTime(playbackTime, elapsedSeconds, replay.playbackRate,
              replay.source.startTime, replay.source.endTime, pausedRef.current);
            if (pausedRef.current) return;
            const frame = frameAtTime(replay.frames, playbackTime);
            latest.current = frame; setSnapshot(frame);
          }, 50);
        } catch (e) {
          if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '저장된 기록을 읽지 못했습니다.');
        }
      };
      void startReplay();
      return () => { abort.abort(); if (timer) clearInterval(timer); };
    }
    let source: EventSource | undefined; let layoutKey = ''; let generation = 0;
    let loadingLayout = false; let pendingSnapshot: Snapshot | null = null;
    const receive = (s: Snapshot) => { if (abort.signal.aborted) return; latest.current = s; receivedAt.current = Date.now(); setConnected(true); setStale(false); setSnapshot(current => !current || !pausedRef.current ? s : current); };
    const start = async () => {
      try {
        const s = await getJson('/api/snapshot', abort.signal);
        if (!isSnapshot(s) || !s.truth || s.scenario === 'runtime') throw Error('이 3D 화면은 창고 시뮬레이션에서 사용할 수 있습니다.');
        const l = await getJson('/api/layout', abort.signal);
        if (!isLayout(l)) throw Error('창고 지도를 읽을 수 없습니다.');
        if (abort.signal.aborted) return;
        layoutKey = s.layout_key; setLayout(l); receive(s);
        source = new EventSource('/api/stream');
        source.addEventListener('logout', () => {
          source?.close(); abort.abort();
          latest.current = null; setSnapshot(null); setLayout(null); setConnected(false);
          setError('세션이 끝났습니다. 2D 관제에서 다시 로그인해 주세요.');
        });
        source.addEventListener('snapshot', async event => {
          try {
            const next = JSON.parse((event as MessageEvent).data);
            if (!isSnapshot(next) || !next.truth || next.scenario === 'runtime') throw Error('시뮬레이션 데이터를 확인할 수 없습니다.');
            pendingSnapshot = next;
            if (next.layout_key !== layoutKey) {
              const revision = ++generation; layoutKey = next.layout_key;
              loadingLayout = true;
              latest.current = null;
              setSnapshot(null); setLayout(null); setSelected(null);
              const nextLayout = await getJson('/api/layout', abort.signal);
              if (!isLayout(nextLayout)) throw Error('변경된 창고 지도를 읽을 수 없습니다.');
              if (revision !== generation || abort.signal.aborted) return;
              setLayout(nextLayout); setCameraView('overview'); setCameraRevision(v => v + 1);
              loadingLayout = false;
              receive(pendingSnapshot ?? next);
              return;
            }
            if (!loadingLayout) receive(next);
          } catch (e) { if (!abort.signal.aborted) {setError(e instanceof Error ? e.message : '데이터를 읽지 못했습니다.');setConnected(false);source?.close();} }
        });
        source.onerror = () => setConnected(false);
      } catch (e) { if (!abort.signal.aborted) setError(e instanceof Error ? e.message : '연결을 확인해 주세요.'); }
    };
    void start();
    const timer = setInterval(() => setStale(receivedAt.current > 0 && Date.now() - receivedAt.current > 5000), 1000);
    return () => { abort.abort(); source?.close(); clearInterval(timer); };
  }, [attempt]);
  useEffect(() => { if (reducedMotion) {pausedRef.current = true; setPaused(true);} }, [reducedMotion]);
  const togglePause = () => { const next = !pausedRef.current; pausedRef.current = next; setPaused(next); if (!next && latest.current) setSnapshot(latest.current); };
  const camera = (view: SceneProps['cameraView']) => { setCameraView(view); setCameraRevision(v => v + 1); };
  const entity = snapshot ? selectedEntity(snapshot, selected) : null;
  const dataReady = !!layout && !!snapshot;
  const fallbackHref = REPLAY_MODE ? PORTFOLIO_URL : '/';
  return <main className="app-shell">
    <header className="topbar">
      <a className="brand" href={fallbackHref}><span className="brand-mark" aria-hidden="true">F</span><span><strong>FLEET CONTROL TOWER</strong><small>창고를 입체적으로, 상태는 한눈에</small></span></a>
      <div className="topbar-actions"><span className="mode-tag">{REPLAY_MODE ? `시뮬레이션 기록 재생 · ${replayRate}배속` : '시뮬레이션 · 읽기 전용'}</span><a className="button secondary" href={fallbackHref}>{REPLAY_MODE ? '포트폴리오로 돌아가기 ↗' : '2D 관제 ↗'}</a></div>
    </header>
    <section className="heading"><div><p className="eyebrow">WAREHOUSE / SPATIAL VIEW</p><h1>물류 흐름이 보이는 3D 창고</h1><p>차량·화물·랙을 입체로 보고, 필요한 구역 가까이에서 살펴보세요.</p></div><div className={`connection ${connected && (REPLAY_MODE || !stale) ? 'is-live' : ''}`} role="status"><span aria-hidden="true" />{REPLAY_MODE ? (error ? '기록 확인 필요' : !dataReady ? '기록 불러오는 중' : paused ? '기록 재생 멈춤' : '기록 재생 중') : (error ? '연결 확인 필요' : !dataReady ? '데이터 연결 중' : paused ? '화면 멈춤' : connected && !stale ? '시뮬레이션 수신 중' : '수신 지연 · 마지막 상태')}<small>{snapshot ? `${Math.floor(snapshot.t / 60)}분 ${Math.floor(snapshot.t % 60)}초 · 시뮬레이션 시간` : REPLAY_MODE ? '저장된 시뮬레이션 기록' : '기존 관제 데이터에 연결합니다'}</small></div></section>
    {error ? <div className="error-banner" role="alert">{error}<button onClick={() => setAttempt(v => v + 1)}>{REPLAY_MODE ? '다시 읽기' : '다시 연결'}</button></div> : null}
    <section className="metrics" aria-label="현재 창고 상태">
      <div><span>관제 차량</span><strong>{snapshot?.robots.length ?? '—'}<small>대</small></strong></div>
      <div><span>작업 중</span><strong>{snapshot?.kpi.working ?? '—'}<small>대</small></strong></div>
      <div><span>랙 보관 팔레트</span><strong>{snapshot ? inventoryTotal(snapshot).toLocaleString('ko-KR') : '—'}<small>개</small></strong></div>
      <div><span>작업 대기</span><strong>{snapshot?.kpi.queued ?? '—'}<small>건</small></strong></div>
      <div><span>활성 알람</span><strong>{snapshot ? snapshot.alarm_counts.p1 + snapshot.alarm_counts.p2 + snapshot.alarm_counts.p3 : '—'}<small>건</small></strong></div>
    </section>
    <div className="workspace">
      <section className="scene-panel" aria-labelledby="scene-title">
        <div className="scene-heading"><div><h2 id="scene-title">창고 전체</h2><span>{layout ? `${layout.width} × ${layout.height} m` : '지도 준비 중'} · 입고 → 보관 → 피킹 → 출고</span></div><button className="button" onClick={togglePause} disabled={!dataReady} aria-pressed={paused}>{paused ? '화면 재생' : '화면 멈춤'}</button></div>
        <div className="camera-bar" role="group" aria-label="카메라 위치">
          {([['overview','전체 창고'],['inbound','입고 도크'],['racks','팔레트 랙'],['outbound','피킹·출고']] as const).map(([view,label]) => <button key={view} disabled={!dataReady} aria-pressed={cameraView === view} onClick={() => camera(view)}>{label}</button>)}
        </div>
        <div className="scene-stage" aria-label="마우스나 터치로 회전·확대할 수 있는 3D 창고. 차량 선택은 옆 목록에서도 가능합니다.">
          {dataReady ? <SceneBoundary fallbackHref={fallbackHref}><Suspense fallback={<div className="scene-message">3D 창고를 불러오는 중입니다.</div>}><WarehouseScene layout={layout} snapshot={snapshot} selected={selected} onSelect={setSelected} cameraView={cameraView} cameraRevision={cameraRevision} showRoutes={showRoutes} showRacks={showRacks} paused={paused} reducedMotion={reducedMotion} /></Suspense></SceneBoundary> : <div className="scene-message"><strong>{error ? (REPLAY_MODE ? '저장된 기록을 확인해 주세요.' : '관제 연결을 확인해 주세요.') : '창고를 준비하고 있습니다.'}</strong><p>{REPLAY_MODE ? '랙과 차량을 저장된 시뮬레이션 기록에서 불러옵니다.' : '랙과 차량을 기존 관제 데이터에서 불러옵니다.'}</p><a href={fallbackHref}>{REPLAY_MODE ? '포트폴리오로 돌아가기' : '2D 관제 열기'}</a></div>}
        </div>
        <footer className="scene-footer"><p>드래그로 회전 · 휠·핀치로 확대 · 차량 클릭으로 선택</p><div className="layer-controls"><label><input type="checkbox" checked={showRoutes} onChange={e => setShowRoutes(e.target.checked)} />차량 경로</label><label><input type="checkbox" checked={showRacks} onChange={e => setShowRacks(e.target.checked)} />랙·보관 화물</label></div></footer>
      </section>
      <aside className="details-panel" aria-label="선택 항목과 화면 설명">
        <div className="details-heading"><p className="eyebrow">OBJECT INSPECTOR</p><h2>차량 살펴보기</h2></div>
        <label className="select-label" htmlFor="vehicle-choice">관제 차량 선택</label>
        <select id="vehicle-choice" value={selected ?? ''} onChange={e => setSelected(e.target.value || null)} disabled={!snapshot}><option value="">차량을 선택하세요</option>{snapshot?.robots.map(r => <option key={r.id} value={r.id}>{r.id} · {r.cls === 'FORKLIFT' ? '자율 지게차' : '토트 AMR'}</option>)}{snapshot?.truth?.forklifts.map(f => <option key={f[0]} value={f[0]}>{f[0]} · 수동 지게차</option>)}{snapshot?.truth?.humans.map(h => <option key={h[0]} value={h[0]}>{h[0]} · 작업자</option>)}</select>
        {entity ? <div className="entity-info"><div className="entity-name"><span>{entity.kind}</span><h3>{entity.id}</h3><p>{entity.label}</p></div><dl><div><dt>위치</dt><dd>{entity.x.toFixed(1)}, {entity.y.toFixed(1)} m</dd></div><div><dt>속도</dt><dd>{entity.speed.toFixed(2)} m/s</dd></div>{entity.robot ? <><div><dt>배터리</dt><dd>{entity.robot.bat.toFixed(0)}%</dd></div><div><dt>적재</dt><dd>{entity.robot.load ? '화물 운반 중' : '빈 차량'}</dd></div></> : null}</dl>{entity.robot?.purpose ? <p className="purpose">{({pickup: '화물 픽업', drop: '화물 하역', charge: '충전', park: '주차', picking: '피킹 작업'} as Record<string,string>)[entity.robot.purpose] ?? '배정된 작업 수행 중'}</p> : null}<button className="button primary" onClick={() => camera('selected')}>선택 차량 따라보기</button><button className="text-button" onClick={() => setSelected(null)}>선택 해제</button></div> : <p className="empty-selection">장면의 차량이나 위 목록을 선택하면 위치·속도·적재 상태가 여기에 표시됩니다.</p>}
        <div className="legend"><h3>형태와 색으로 구분합니다</h3><ul><li><span className="swatch blue" />자율 지게차 · 포크로 팔레트 운반</li><li><span className="swatch teal" />토트 AMR · 피킹 상자 운반</li><li><span className="swatch amber" />수동 지게차 · 시뮬레이터 위치</li><li><span className="swatch cargo" />팔레트·박스 · 적재 수량 표시</li></ul></div>
        {snapshot ? <div className="fleet-status"><h3>차량 상태</h3>{(['moving','idle','attention','offline'] as const).map((key,i) => <div key={key}><span>{['이동 중','정차·대기','주의 상태','통신 끊김'][i]}</span><b>{snapshot.robots.filter(r => robotStatusClass(r) === key).length}대</b></div>)}</div> : null}
      </aside>
    </div>
    <section className="evidence-note" aria-label="3D 표시 기준"><strong>{REPLAY_MODE ? '저장된 시뮬레이션 기록, 입체적인 표현' : '같은 관제 데이터, 입체적인 표현'}</strong><p>차량 위치·방향·적재 상태와 랙의 팔레트 수량을 시뮬레이션에서 가져옵니다. 랙 높이·차량 외형·박스 크기·보관 위치의 세부 배치는 이해를 돕는 표현입니다. 수동 지게차와 작업자는 시뮬레이터 좌표를 사용하며, 2D 관제의 센서 추정값과 구분합니다.</p><p>{REPLAY_MODE ? '이 화면은 저장된 기록만 재생하며 관제 서버나 차량에 연결하거나 제어 명령을 보내지 않습니다.' : '화면 멈춤은 이 화면의 표시만 멈춥니다. 실제 차량이나 시뮬레이션에 제어 명령을 보내지 않습니다.'}</p></section>
  </main>;
}
