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
  await sleep(3000);
  const diag = await evaluate(`Promise.race([new Promise(r => requestAnimationFrame(() => r('raf ok'))), new Promise(r => setTimeout(() => r('raf timeout'), 2000))]).then(raf => ({raf, res: performance.getEntriesByType('resource').filter(e => e.name.includes('/api/')).map(e => e.name.replace(location.origin, '') + ':' + (e.responseStatus ?? '?')), canvas: [document.getElementById('map').width, document.getElementById('map').height, document.getElementById('map').clientWidth, document.getElementById('map').clientHeight]}))`);
  check('레이아웃 수신', diag.res.some(r => r.startsWith('/api/layout:200')), diag.res.join(' '));
  const px = await evaluate(`(() => { const c = document.getElementById('map'); const d = c.getContext('2d').getImageData(Math.floor(c.width/2), Math.floor(c.height/2), 1, 1).data; return [...d]; })()`);
  check('지도 캔버스에 그림이 있음', !(px[3] === 0), JSON.stringify(px));
  await shot('02_main.png');
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
console.log(`${results.filter(r => r.ok).length}/${results.length}`);
process.exit(results.every(r => r.ok) ? 0 : 1);
