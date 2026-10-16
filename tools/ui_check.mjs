// Screen check in headless Chrome over the DevTools protocol (no extra packages, no window on the screen):
// login (wrong then right password), vehicles and KPIs from the stream, the map drawn, a command with and
// without the CSRF token, logout, and no page exceptions. Screenshots go to OUT_DIR.
//
//   FCT_ADMIN_PASSWORD=... python3 -m fleet.server --no-jdamr --port 18100 --db /tmp/fct-ui.sqlite3 &
//   node tools/ui_check.mjs http://127.0.0.1:18100/ admin "$FCT_ADMIN_PASSWORD" /tmp/fct-ui
//
// CHROME selects the browser binary (default google-chrome).
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [,, base, user, pass, outDir] = process.argv;
if (!base || !user || !pass || !outDir) {
  console.error('usage: node tools/ui_check.mjs <base-url> <user> <password> <out-dir>');
  process.exit(2);
}
const port = Number(process.env.CDP_PORT || 9339);
mkdirSync(outDir, { recursive: true });
const prof = mkdtempSync(join(outDir, 'chrome-'));
const chrome = spawn(process.env.CHROME || 'google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`,
  '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--window-size=1600,1000', 'about:blank'],
  { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws, id = 0; const pending = new Map(); const events = [];
async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find(t => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(200);
  }
  throw new Error('chrome did not start');
}
function send(method, params = {}) {
  return new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
}
async function evaluate(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result.value;
}
async function waitFor(expr, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { if (await evaluate(expr)) return true; } catch {}       // the page may still be loading
    await sleep(200);
  }
  return false;
}
async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(outDir, name), Buffer.from(r.data, 'base64'));
}
async function click(selector) {
  const point = await evaluate(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); e.scrollIntoView({block:'nearest'}); const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
}
async function viewport(width, height, scale = 1) {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: scale, mobile: false });
  await sleep(300);
}
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); };
try {
  ws = new WebSocket(await connect());
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
    else if (m.method) events.push(m);
  });
  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Page.navigate', { url: base });
  await waitFor(`document.readyState === 'complete' && !!document.getElementById('login-veil')`);
  check('로그인 화면이 먼저 뜸', await waitFor(`!document.getElementById('login-veil').hidden`));
  await shot('01_login.png');
  // wrong password first
  await evaluate(`document.getElementById('login-name').value=${JSON.stringify(user)}; document.getElementById('login-pass').value='wrong-password-x'; document.getElementById('login-form').requestSubmit(); true`);
  check('틀린 비밀번호는 오류 문구', await waitFor(`document.getElementById('login-error').textContent.length > 0`), await evaluate(`document.getElementById('login-error').textContent`));
  await evaluate(`document.getElementById('login-pass').value=${JSON.stringify(pass)}; document.getElementById('login-form').requestSubmit(); true`);
  check('로그인 뒤 가림막 사라짐', await waitFor(`document.getElementById('login-veil').hidden`));
  check('계정 표시', await waitFor(`document.getElementById('account').textContent.includes(${JSON.stringify(user)})`), await evaluate(`document.getElementById('account').textContent`));
  check('차량 목록 채워짐(스냅숏 수신)', await waitFor(`document.querySelectorAll('#fleet-rows tr').length > 0`, 20000), String(await evaluate(`document.querySelectorAll('#fleet-rows tr').length`)));
  check('운영 모드 표시', await waitFor(`document.getElementById('mode').textContent !== '연결 대기'`), await evaluate(`document.getElementById('mode').textContent`));
  check('선택 전략과 현재 상태의 이름 일치', await evaluate('document.getElementById("subtitle").textContent.includes(document.getElementById("strategy").selectedOptions[0].textContent)'));
  await sleep(3000);
  const diag = await evaluate(`Promise.race([new Promise(r => requestAnimationFrame(() => r('raf ok'))), new Promise(r => setTimeout(() => r('raf timeout'), 2000))]).then(raf => ({raf, res: performance.getEntriesByType('resource').filter(e => e.name.includes('/api/')).map(e => e.name.replace(location.origin, '') + ':' + (e.responseStatus ?? '?')), canvas: [document.getElementById('map').width, document.getElementById('map').height, document.getElementById('map').clientWidth, document.getElementById('map').clientHeight]}))`);
  check('레이아웃 수신', diag.res.some(r => r.startsWith('/api/layout:200')), diag.res.join(' '));
  const px = await evaluate(`(() => { const c = document.getElementById('map'); const d = c.getContext('2d').getImageData(Math.floor(c.width/2), Math.floor(c.height/2), 1, 1).data; return [...d]; })()`);
  check('지도 캔버스에 그림이 있음', !(px[3] === 0), JSON.stringify(px));
  await shot('02_main.png');
  // Observe real canvas placement, without changing its returned positions or drawing.
  await evaluate(`(() => {
    const place = FleetLogic.placeMapLabels, apply = FleetLogic.applyView;
    FleetLogic.placeMapLabels = (...args) => { const result = place(...args); window.__mapLabels = {placed:result,obstacles:args[1],width:args[2],height:args[3]}; return result; };
    FleetLogic.applyView = (...args) => { const result = apply(...args); window.__mapTransform = result; return result; };
    return true;
  })()`);
  await evaluate('fetch("/api/layout").then(r=>r.json()).then(d=>{window.__mapWorld=d;return true;})');
  const bounds = id => evaluate(`(() => { const r=document.getElementById(${JSON.stringify(id)}).getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}; })()`);
  const labelsClear = () => evaluate(`(() => {
    const d=window.__mapLabels; if (!d || (d.width>=650 && !d.placed.length)) return false;
    const overlap=(a,b)=>a.x<b.x+b.w && a.x+a.w>b.x && a.y<b.y+b.h && a.y+a.h>b.y;
    return d.placed.every((l,i)=>l.box.x>=0 && l.box.y>=0 && l.box.x+l.box.w<=d.width && l.box.y+l.box.h<=d.height && !d.obstacles.concat(d.placed.slice(0,i).map(x=>x.box)).some(b=>overlap(l.box,b)));
  })()`);
  const sceneFits = () => evaluate(`(()=>{const c=document.getElementById('map'),t=window.__mapTransform,w=window.__mapWorld,r=c.getBoundingClientRect(),p=document.getElementById('map-panel').getBoundingClientRect();const a=FleetLogic.toScreen(t,0,w.height),b=FleetLogic.toScreen(t,w.width,0);return {fits:r.left>=p.left&&r.right<=p.right&&r.right<=innerWidth&&a[0]>=0&&a[1]>=58&&b[0]<=c.clientWidth+1&&b[1]<=c.clientHeight+1,left:a[0],top:a[1],right:b[0],bottom:b[1],width:c.clientWidth,height:c.clientHeight,panelWidth:p.width,viewport:innerWidth};})()`);
  const leadersClear = () => evaluate(`(() => {
    const d=window.__mapLabels;if(!d)return false;
    const inside=(r,x,y)=>x>=r.x&&x<=r.x+r.w&&y>=r.y&&y<=r.y+r.h;
    return d.placed.every(l=>Array.from({length:51},(_,i)=>i/50).every(t=>{
      const x=l.leader.from[0]*(1-t)+l.leader.to[0]*t,y=l.leader.from[1]*(1-t)+l.leader.to[1]*t;
      return !d.obstacles.concat(d.placed.filter(p=>p.id!==l.id).map(p=>p.box)).some(r=>inside(r,x,y));
    }));
  })()`);
  check('지도 기호 6종과 선 설명 표시', await evaluate(`document.querySelectorAll('.symbol-key>div').length===6 && document.querySelectorAll('.line-key>span').length===5`));
  check('데스크톱 라벨이 유닛·다른 글자를 가리지 않음', await waitFor(`!!window.__mapLabels`) && await labelsClear());
  const desktopFit = await sceneFits();
  check('전체 맞춤에서 현장 전체가 지도 안에 표시', desktopFit.fits, JSON.stringify(desktopFit));
  check('라벨 연결선이 기호·랙·다른 글자를 가리지 않음', await leadersClear());
  const oldScale = await evaluate('window.__mapTransform.scale');
  await click('#map-zoom-in'); await sleep(200);
  check('확대 버튼이 실제 지도 배율을 키움', await evaluate('document.getElementById("map-zoom").textContent') === '125%' && Math.abs(await evaluate('window.__mapTransform.scale') / oldScale - 1.25) < .001);
  await click('#map-zoom-out'); await sleep(200);
  check('축소 버튼이 배율을 되돌림', await evaluate('document.getElementById("map-zoom").textContent') === '100%');
  for (let i=0;i<10;i++) await click('#map-zoom-in');
  check('최대 확대에서 버튼 비활성화', await evaluate('document.getElementById("map-zoom-in").disabled && document.getElementById("map-zoom").textContent==="600%"'));
  await click('#map-fit');
  for (let i=0;i<5;i++) await click('#map-zoom-out');
  check('최소 축소에서 버튼 비활성화', await evaluate('document.getElementById("map-zoom-out").disabled && document.getElementById("map-zoom").textContent==="60%"'));
  await click('#map-fit'); await sleep(150);
  const beforePan = await evaluate('window.__mapTransform.ox');
  await evaluate(`window.__mapKeyBubble=0; window.__mapKeyProbe=()=>window.__mapKeyBubble++; document.addEventListener('keydown',window.__mapKeyProbe); document.addEventListener('keyup',window.__mapKeyProbe); true`);
  await evaluate('document.getElementById("map").focus(); true');
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowLeft', code: 'ArrowLeft' });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowLeft', code: 'ArrowLeft' }); await sleep(150);
  check('키보드 방향키로 지도 이동', Math.abs(await evaluate('window.__mapTransform.ox') - beforePan - 40) < .01);
  check('지도 방향키가 차량 원격 조종으로 전달되지 않음', await evaluate('window.__mapKeyBubble') === 0);
  await evaluate(`document.removeEventListener('keydown',window.__mapKeyProbe); document.removeEventListener('keyup',window.__mapKeyProbe); true`);
  await click('#map-fit'); await sleep(150);
  check('전체 맞춤이 이동과 배율 초기화', await evaluate('document.getElementById("map-zoom").textContent') === '100%' && Math.abs(await evaluate('window.__mapTransform.ox') - beforePan) < .01);
  await evaluate('document.getElementById("map").focus(); true');
  for (let i=0;i<7;i++) {
    await send('Input.dispatchKeyEvent', { type:'keyDown',key:'ArrowDown',code:'ArrowDown' });
    await send('Input.dispatchKeyEvent', { type:'keyUp',key:'ArrowDown',code:'ArrowDown' });
  }
  await sleep(150);
  check('이동 중 지도 기호가 상단 설명 영역을 가리지 않음', await evaluate(`(()=>{const c=document.getElementById('map'),ratio=c.width/c.clientWidth,d=c.getContext('2d').getImageData(0,0,c.width,Math.floor(14*ratio)).data;for(let i=0;i<d.length;i+=4)if(d[i]!==248||d[i+1]!==250||d[i+2]!==252)return false;return true;})()`));
  await click('#map-fit'); await sleep(150);
  const defaultWide = await bounds('map');
  check('처음 열면 넓게 보기와 아래 보조 패널이 기본', await evaluate('document.querySelector(".work").classList.contains("map-wide") && document.getElementById("map-wide").getAttribute("aria-expanded")==="true" && document.getElementById("map-wide").textContent==="패널 나란히 보기" && document.querySelector(".side").getBoundingClientRect().top >= document.getElementById("map-panel").getBoundingClientRect().bottom'));
  await click('#map-wide'); await sleep(200);
  const beside = await bounds('map');
  check('패널 나란히 보기로 지도 폭이 줄고 보조 패널이 옆으로 이동', defaultWide.width > beside.width * 1.1 && await evaluate('document.getElementById("map-wide").getAttribute("aria-expanded")==="false" && document.getElementById("map-wide").textContent==="넓게 보기" && document.querySelector(".side").getBoundingClientRect().left >= document.getElementById("map-panel").getBoundingClientRect().right'), `${defaultWide.width} → ${beside.width}px`);
  await click('#map-wide'); await sleep(150);
  check('넓게 보기로 기본 폭과 아래 보조 패널 복귀', Math.abs((await bounds('map')).width - defaultWide.width) < 1 && await evaluate('document.getElementById("map-wide").getAttribute("aria-expanded")==="true" && document.querySelector(".side").getBoundingClientRect().top >= document.getElementById("map-panel").getBoundingClientRect().bottom'));
  await shot('03_wide.png');
  const canvasBox = await bounds('map');
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: canvasBox.x + canvasBox.width / 2, y: canvasBox.y + 100, deltaY: -120, deltaX: 0 }); await sleep(150);
  check('마우스 휠 확대', await evaluate('document.getElementById("map-zoom").textContent') === '115%');
  await click('#map-fit');
  // Half the desktop CSS viewport checks 200%-equivalent reflow; DPR alone does not prove browser zoom.
  for (const [name, w, h, scale] of [['tablet',1024,1000,1],['mobile',390,1000,1],['zoom200_layout',800,500,2]]) {
    await viewport(w,h,scale);
    await click('#map-fit'); await sleep(150);
    await evaluate('document.getElementById("map-panel").scrollIntoView({block:"start"}); true'); await sleep(150);
    check(`${name}: 문서 가로 넘침 없음`, await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
    check(`${name}: 지도 라벨 겹침 없음`, await labelsClear());
    check(`${name}: 연결선이 기호·글자를 가리지 않음`, await leadersClear());
    check(`${name}: 지도 제어가 화면 안에 들어옴`, await evaluate('[...document.querySelectorAll(".map-tools button")].filter(e=>getComputedStyle(e).display!=="none").every(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.width>=44&&r.height>=44;})'));
    const fit = await sceneFits();
    check(`${name}: 전체 맞춤에서 현장 잘림 없음`, fit.fits, JSON.stringify(fit));
    await shot(`04_${name}.png`);
  }
  await viewport(1600,1800);
  await evaluate('window.scrollTo(0,0); true'); await sleep(200);
  await click('#layers [data-k="ids"]'); await sleep(150);
  check('ID 레이어를 끄면 차량 이름이 사라짐', await evaluate('document.querySelector("#layers [data-k=ids]").getAttribute("aria-pressed")==="false" && !window.__mapLabels.placed.some(l=>l.id.startsWith("robot-"))'));
  await click('#layers [data-k="ids"]'); await sleep(150);
  check('ID 레이어를 켜면 차량 이름 복귀', await evaluate('window.__mapLabels.placed.some(l=>l.id.startsWith("robot-"))'));
  const openLegendHeight = await evaluate('document.querySelector(".map-key").getBoundingClientRect().height');
  await click('.map-key summary');
  const closedLegend = await evaluate('(()=>{const d=document.querySelector(".map-key");return {open:d.open,height:d.getBoundingClientRect().height,summary:d.querySelector("summary").getBoundingClientRect().height};})()');
  check('범례 접기가 내용을 숨김', !closedLegend.open && closedLegend.height <= closedLegend.summary + 2 && closedLegend.height < openLegendHeight - 50, JSON.stringify(closedLegend));
  await evaluate('document.querySelector(".map-key summary").focus(); true');
  await send('Input.dispatchKeyEvent', { type:'keyDown',key:' ',code:'Space',text:' ' });
  await send('Input.dispatchKeyEvent', { type:'keyUp',key:' ',code:'Space' });
  check('범례 펼치기가 그림 설명을 복원', await evaluate('document.querySelector(".map-key").open && document.querySelector(".symbol-key").getBoundingClientRect().height>0'));
  await evaluate('window.scrollTo(0,0); true');
  // Keep the snapshot target still while the stream and drawing interpolation settle.
  const wasPaused = await evaluate('fetch("/api/snapshot").then(r=>r.json()).then(s=>s.run.paused)');
  const setPause = value => evaluate(`fetch('/api/me').then(r=>r.json()).then(me=>fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf},body:JSON.stringify({action:'pause',value:${value}})})).then(r=>r.status)`);
  if (!wasPaused && await setPause(true) !== 200) throw new Error('hover setup: pause failed');
  try {
    await sleep(800);
    const unitPoint = await evaluate(`fetch('/api/snapshot').then(r=>r.json()).then(s=>{const c=document.getElementById('map').getBoundingClientRect();for(const r of s.robots){const [x,y]=FleetLogic.toScreen(window.__mapTransform,r.x,r.y);if(y+c.y>c.y+60&&y+c.y<innerHeight-50)return {x:x+c.x,y:y+c.y,id:r.id};}return null;})`);
    if (unitPoint) {
      const beforeHover = await bounds('layers');
      await send('Input.dispatchMouseEvent', { type:'mouseMoved',x:unitPoint.x,y:unitPoint.y }); await sleep(100);
      const afterHover = await bounds('layers'), hint = await evaluate('document.getElementById("tip").textContent');
      check('호버 안내가 지도 아래에 표시되고 레이아웃은 고정', Math.abs(afterHover.y - beforeHover.y) < 1 && hint.includes(unitPoint.id), JSON.stringify({before:beforeHover.y,after:afterHover.y,hint}));
      await send('Input.dispatchMouseEvent', { type:'mousePressed',x:unitPoint.x,y:unitPoint.y,button:'left',clickCount:1 });
      await send('Input.dispatchMouseEvent', { type:'mouseReleased',x:unitPoint.x,y:unitPoint.y,button:'left',clickCount:1 }); await sleep(150);
      check('지도에서 차량 선택 시 상세 정보 갱신', await evaluate('document.querySelector(".unit-head strong")?.textContent') === unitPoint.id);
    } else check('선택 가능한 지도 차량 있음', false);
  } finally {
    if (!wasPaused && await setPause(false) !== 200) throw new Error('hover cleanup: resume failed');
  }
  await sleep(300);
  await shot('05_overview.png');
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  check('축소 동작 설정에서 UI 전환 모션 제거', await evaluate('getComputedStyle(document.getElementById("toast")).transitionDuration') === '0s');
  await send('Emulation.setEmulatedMedia', { features: [] });
  await viewport(1600,1000); await evaluate('window.scrollTo(0,0); true');
  const kpi = await evaluate(`[...document.querySelectorAll('#kpis .kpi')].slice(0, 20).map(e => e.textContent.trim().replace(/\\s+/g, ' ')).join(' | ')`);
  check('KPI 표시', kpi.length > 0, kpi.slice(0, 400));
  const estop = await evaluate(`(document.getElementById('fleet-estop')||{}).textContent || ''`);
  check('일괄 정지 버튼 문구', estop.includes('비안전'), estop);
  // a state-changing call from the page: the CSRF header is attached by api()
  const r = await evaluate(`fetch('/api/me').then(r => r.json()).then(me => fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token': me.csrf}, body: JSON.stringify({cmd:'fleet_hold', confirm:true})})).then(r => r.status)`);
  check('CSRF 토큰으로 명령 성공', r === 200, String(r));
  const r2 = await evaluate(`fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cmd:'fleet_resume', confirm:true})}).then(r => r.status)`);
  check('CSRF 토큰 없는 명령 거부', r2 === 403, String(r2));
  await evaluate(`document.getElementById('logout').click(); true`);
  check('로그아웃 뒤 로그인 화면', await waitFor(`!document.getElementById('login-veil').hidden`));
  const errs = events.filter(e => e.method === 'Runtime.exceptionThrown').map(e => e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text);
  const consoleErr = events.filter(e => e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error').map(e => e.params.args.map(a => a.value ?? a.description).join(' '));
  check('페이지 예외 없음', errs.length === 0, errs.join(' / ').slice(0, 400));
  check('콘솔 오류는 의도한 401·403뿐', consoleErr.every(t => /401|403/.test(t)), consoleErr.join(' / ').slice(0, 400));
} catch (e) {
  check('실행', false, String(e));
} finally {
  try { ws?.close(); } catch {}
  chrome.kill('SIGTERM');
  await sleep(500);
  rmSync(prof, { recursive: true, force: true });
}
for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'} ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
writeFileSync(join(outDir,'checks.json'), JSON.stringify(results,null,2)+'\n');
console.log(`${results.filter(r => r.ok).length}/${results.length}`);
process.exit(results.every(r => r.ok) ? 0 : 1);
