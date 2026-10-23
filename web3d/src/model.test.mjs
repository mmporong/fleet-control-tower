import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inventoryTotal, isLayout, isSnapshot, selectedEntity, robotStatusClass } from './model.ts';
const robot = {id:'AGF-01',cls:'FORKLIFT',x:4,y:8,th:0,v:0,st:'IDLE',label:'대기',bat:90,load:true,conn:'ONLINE',base:[],route:[],positioned:true};
const snapshot = {t:10,layout_key:'standard:0',scenario:'standard',robots:[robot],site_state:{rack:{'R1-01':3,'R1-02':7},in_stage:{SI1:2}},kpi:{working:0},alarm_counts:{p1:0,p2:0,p3:0},truth:{forklifts:[['F-1',12,15,1.2,0.4]],humans:[['H-1',20,30,'picker',true,0.6]]}};
test('랙 집계는 실제 랙 수량만 합산하고 스테이징·가상 슬롯을 더하지 않는다',()=>{assert.equal(inventoryTotal(snapshot),10);assert.equal(inventoryTotal({...snapshot,site_state:{}}),0);});
test('동일 선택 함수가 관제 차량과 시뮬레이터 지게차·작업자 좌표를 구분한다',()=>{assert.equal(selectedEntity(snapshot,'AGF-01').robot.load,true);assert.deepEqual(selectedEntity(snapshot,'F-1'),{id:'F-1',kind:'수동 지게차',x:12,y:15,speed:0.4,label:'시뮬레이터 위치·방향',robot:null});assert.equal(selectedEntity(snapshot,'H-1').y,30);assert.equal(selectedEntity(snapshot,'missing'),null);});
test('잘못된 수신 데이터와 유한하지 않은 위치를 장면에 전달하지 않는다',()=>{assert.equal(isSnapshot(snapshot),true);assert.equal(isSnapshot({error:'unauthorized'}),false);assert.equal(isSnapshot({...snapshot,robots:[{...robot,x:NaN}]}),false);assert.equal(isLayout({width:88,height:53.5,nodes:[],edges:[],racks:[],zones:[]}),true);assert.equal(isLayout({width:0,height:53.5,nodes:[],edges:[],racks:[],zones:[]}),false);});
test('통신 두절과 주의 상태를 이동 속도보다 먼저 표시한다',()=>{assert.equal(robotStatusClass({...robot,conn:'OFFLINE',v:1}),'offline');assert.equal(robotStatusClass({...robot,st:'PROTECTIVE_STOP',v:1}),'attention');assert.equal(robotStatusClass({...robot,v:1}),'moving');assert.equal(robotStatusClass(robot),'idle');});
