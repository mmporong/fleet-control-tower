import { test } from 'node:test';
import assert from 'node:assert/strict';
import { advanceReplayTime, frameAtTime, isReplayDocument } from './replay.ts';

const layout = {width:88,height:53.5,site:'demo',nodes:[],edges:[],racks:[],walls:[],zones:[],doors:[]};
const frame = t => ({t,layout_key:'standard:0',scenario:'standard',scenario_title:'표준',mode:'simulation',robots:[],site_state:{},kpi:{working:0,queued:0,done:0},alarm_counts:{p1:0,p2:0,p3:0}});
const replay = {version:1,playbackRate:8,sampleInterval:0.5,layout,frames:[frame(120),frame(120.7),frame(122.4),frame(240)],source:{scenario:'standard',seed:7,strategy:'locks+resolve',startTime:120,endTime:240}};

test('기록 파일 계약과 시간순 프레임을 검증한다', () => {
  assert.equal(isReplayDocument(replay), true);
  assert.equal(isReplayDocument({...replay, version:2}), false);
  assert.equal(isReplayDocument({...replay, playbackRate:4}), false);
  assert.equal(isReplayDocument({...replay, frames:[frame(120), frame(119)]}), false);
  assert.equal(isReplayDocument({...replay, source:{...replay.source, strategy:'other'}}), false);
});

test('불규칙한 시각에서도 현재 시각을 넘지 않은 최신 프레임을 찾는다', () => {
  assert.equal(frameAtTime(replay.frames, 120).t, 120);
  assert.equal(frameAtTime(replay.frames, 121.8).t, 120.7);
  assert.equal(frameAtTime(replay.frames, 239.9).t, 122.4);
});

test('8배속 재생 시각은 기록 끝에서 처음으로 순환한다', () => {
  assert.equal(advanceReplayTime(120, 1, 8, 120, 240, false), 128);
  assert.equal(advanceReplayTime(238, 1, 8, 120, 240, false), 126);
});

test('일시 정지하면 경과 시간이 있어도 재생 시각이 변하지 않는다', () => {
  assert.equal(advanceReplayTime(173.5, 30, 8, 120, 240, true), 173.5);
});
