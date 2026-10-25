import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  adjacentMajorEvent, advanceReplayClock, frameAtOrAfterTime, frameAtTime, isReplayDocument, isReplayIndex,
  loadReplayDocument, loadReplayIndex, matchingComparison, recordingById, timelineEvents,
} from './replay.ts';

const layout = {width:88,height:53.5,site:'demo',nodes:[],edges:[],racks:[],walls:[],zones:[],doors:[]};
const frame = t => ({t,layout_key:'standard:0',scenario:'standard',scenario_title:'표준',mode:'simulation',robots:[],site_state:{},kpi:{working:0,queued:0,done:0},alarm_counts:{p1:0,p2:0,p3:0}});
const replay = {version:1,playbackRate:8,sampleInterval:0.5,layout,frames:[frame(120),frame(120.7),frame(122.4),frame(240)],source:{scenario:'standard',seed:7,strategy:'locks+resolve',startTime:120,endTime:240,commit:'abc123'}};

test('기록 파일 계약과 시간순 프레임을 검증한다', () => {
  assert.equal(isReplayDocument(replay), true);
  assert.equal(isReplayDocument({...replay, version:2}), false);
  assert.equal(isReplayDocument({...replay, playbackRate:4}), true);
  assert.equal(isReplayDocument({...replay, playbackRate:0}), false);
  assert.equal(isReplayDocument({...replay, frames:[frame(120), frame(119)]}), false);
  assert.equal(isReplayDocument({...replay, source:{...replay.source, strategy:'other'}}), false);
});

test('불규칙한 시각에서도 현재 시각을 넘지 않은 최신 프레임을 찾는다', () => {
  assert.equal(frameAtTime(replay.frames, 120).t, 120);
  assert.equal(frameAtTime(replay.frames, 121.8).t, 120.7);
  assert.equal(frameAtTime(replay.frames, 239.9).t, 122.4);
});

test('프레임 사이 사건은 사건 뒤 첫 관측 프레임으로 이동한다', () => {
  assert.equal(frameAtOrAfterTime(replay.frames, 120.4).t, 120.7);
  assert.equal(frameAtOrAfterTime(replay.frames, 240.2).t, 240);
});

test('선택한 배속으로 진행하고 정지하면 재생 시각을 유지한다', () => {
  for (const rate of [.5, 1, 2, 4, 8]) {
    assert.deepEqual(advanceReplayClock({time:120,carry:0}, 1, rate, 120, 240, false), {time:120+rate,carry:0});
  }
  assert.deepEqual(advanceReplayClock({time:173.5,carry:0}, 30, 8, 120, 240, true), {time:173.5,carry:0});
  assert.deepEqual(advanceReplayClock({time:232,carry:0}, 1, 8, 120, 240, false), {time:240,carry:0});
});

test('끝 프레임을 한 번 표시한 뒤 초과 시간을 다음 틱에 이어 재생한다', () => {
  const end = advanceReplayClock({time:239.9,carry:0}, .05, 8, 120, 240, false);
  assert.equal(end.time, 240);
  assert.ok(Math.abs(end.carry - .3) < 1e-9);
  const resumed = advanceReplayClock(end, .05, 8, 120, 240, false);
  assert.equal(resumed.carry, 0);
  assert.ok(Math.abs(resumed.time - 120.7) < 1e-9);
  assert.deepEqual(advanceReplayClock(end, 10, 8, 120, 240, true), end);
  const large = advanceReplayClock({time:120,carry:0}, 31, 8, 120, 240, false);
  assert.deepEqual(large, {time:240,carry:8});
  assert.deepEqual(advanceReplayClock(large, .125, 8, 120, 240, false), {time:129,carry:0});
});

test('기록 목록은 상대 경로와 고유 ID만 허용한다', () => {
  const index = {version:1,recordings:[
    {id:'before',title:'개선 전',description:'기준선',url:'replay-before.json',variant:'before'},
    {id:'after',title:'개선 후',description:'개선안',url:'./replay-after.json',variant:'after'},
  ]};
  assert.equal(isReplayIndex(index), true);
  assert.equal(recordingById(index, 'after')?.title, '개선 후');
  assert.equal(isReplayIndex({...index,recordings:[{...index.recordings[0],url:'https://evil.example/replay.json'}]}), false);
  assert.equal(isReplayIndex({...index,recordings:[index.recordings[0],index.recordings[0]]}), false);
});

test('동일 비교 그룹과 같은 측정 범위일 때만 개선 전후를 묶는다', () => {
  const summary = {durationSeconds:3600,warmupSeconds:600,shippedPerHour:10};
  const records = [
    {id:'reference',title:'표준',description:'',url:'reference.json',variant:'reference'},
    {id:'a-before',title:'A 전',description:'',url:'a-before.json',variant:'before',group:'a',summary},
    {id:'b-after',title:'B 후',description:'',url:'b-after.json',variant:'after',group:'b',summary},
  ];
  assert.equal(matchingComparison(records, records[0]), null);
  const matching = [...records, {id:'a-after',title:'A 후',description:'',url:'a-after.json',variant:'after',group:'a',summary}];
  assert.equal(matchingComparison(matching, matching[0])?.after.id, 'a-after');
  const mismatchedDuration = matching.map(item => item.id === 'a-after' ? {...item,summary:{...summary,durationSeconds:7200}} : item);
  assert.equal(matchingComparison(mismatchedDuration, mismatchedDuration[1]), null);
});

test('목록 404만 기존 replay.json 기록으로 대체한다', async () => {
  const fallback = await loadReplayIndex('https://fleet.example/demo/', new AbortController().signal,
    async () => new Response('', {status:404}));
  assert.equal(fallback.recordings[0].url, 'replay.json');
  await assert.rejects(() => loadReplayIndex('https://fleet.example/demo/', new AbortController().signal,
    async () => new Response('', {status:500})), /기록 목록을 불러오지 못했습니다/);
});

test('기록 전환은 선택한 상대 경로를 읽고 이전 요청 중단을 전달한다', async () => {
  const controller = new AbortController();
  let aborted = false;
  const pending = loadReplayDocument('https://fleet.example/demo/',
    {id:'before',title:'개선 전',description:'',url:'before.json',variant:'before'}, controller.signal,
    async (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('aborted', 'AbortError')); });
    }));
  controller.abort();
  await assert.rejects(pending, {name:'AbortError'});
  assert.equal(aborted, true);

  let requested = '';
  const loaded = await loadReplayDocument('https://fleet.example/demo/',
    {id:'after',title:'개선 후',description:'',url:'after.json',variant:'after'}, new AbortController().signal,
    async url => { requested = String(url); return new Response(JSON.stringify(replay), {status:200}); });
  assert.equal(requested, 'https://fleet.example/demo/after.json');
  assert.equal(loaded.frames.length, replay.frames.length);
});

test('모든 프레임의 사건을 중복 제거하고 가까운 반복은 횟수로 접는다', () => {
  const blocked = {t:121,kind:'blocked',source:'AGF-01',msg:'N-17 진입 대기',level:'warning'};
  const resolved = {t:140,kind:'resolve',source:'AGF-01',msg:'우회 경로 적용',level:'info'};
  const events = timelineEvents([
    {...frame(120),events:[{...blocked,t:0},blocked]},
    {...frame(130),events:[blocked,{...blocked,t:126},resolved]},
    {...frame(140),events:[resolved]},
  ]);
  assert.equal(events.length, 2);
  assert.equal(events.some(event => event.firstTime === 0), false);
  assert.equal(events[0].count, 2);
  assert.equal(events[0].lastTime, 126);
  assert.equal(adjacentMajorEvent(events, 120, 1)?.kind, 'blocked');
  assert.equal(adjacentMajorEvent(events, 130, -1)?.kind, 'blocked');
});
