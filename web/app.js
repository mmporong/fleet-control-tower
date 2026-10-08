/* AMR 관제탑 UI. Talks to fleet.server: SSE snapshots in, audited commands out. */
(() => {
  'use strict';
  const L = window.FleetLogic;
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage unavailable */ } },
  };

  const S = {
    layout: null, layoutKey: null, snap: null, prev: null, snapAt: 0, meta: null,
    sel: null, view: { zoom: 1, panX: 0, panY: 0 }, openAlarm: null, tab: store.get('fct.tab', 'fleet'),
    layers: Object.assign({ locks: true, routes: true, wait: true, predict: true, zones: true, heat: false, cams: false, truth: false, ids: true },
      store.get('fct.layers', {})),
    vda: { robot: null, msgs: [], sel: 0 }, replay: { data: null, idx: 0, playing: false, timer: null },
    teleop: { keys: new Set(), timer: null, local: null, localTimer: null }, history: [], connected: false,
    checked: {},
  };
  const LAYERS = [['locks', '예약'], ['routes', '경로'], ['wait', '대기 관계'], ['predict', '사람·지게차 예측'], ['zones', '구역'],
    ['heat', '혼잡'], ['cams', '카메라 범위'], ['truth', '실제 위치(평가)'], ['ids', 'ID']];
  const TABS = [['fleet', '차량 목록'], ['site', '현장 현황'], ['tasks', '작업 큐'], ['zones', '구역 제어'], ['incidents', '사고 기록·재생'], ['log', '이벤트·감사'],
    ['vda', 'VDA 5050 검사기'], ['jdamr', '실물 JD-AMR'], ['drill', '훈련·시나리오'], ['trend', '추세']];
  const CMD_LABEL = {
    zone_lock: '구역 봉쇄', zone_unlock: '구역 봉쇄 해제', set_mode_manual: '원격 조종 인계', set_mode_auto: '자동 복귀',
    field_service_done: '현장 조치 완료', resume_area: '주변 재개', reset_fault: '원격 리셋', resume_robot: '로봇 재개',
    cancel_task: '작업 취소', send_to_park: '주차 보내기', send_to_charge: '충전 보내기', reassign_task: '작업 재할당',
    init_position: '위치 재지정', fleet_estop_release: '일괄 정지 해제', fleet_resume: '전체 재개', fleet_hold: '전체 일시정지',
    zone_speed: '구역 속도(구역 탭)', set_task_rate: '투입률(작업 탭)', reprioritize_task: '우선순위(작업 탭)',
    door_disable: '문 사용 중지', door_retry: '문 재시도', door_field_fix: '[현장 모의] 문 수리',
  };
  const CONFIRM = new Set(['fleet_estop', 'fleet_estop_release', 'fleet_hold', 'fleet_resume', 'zone_evacuate', 'zone_lock', 'set_mode_manual']);

  // ------------------------------------------------------------------ network
  // session: the server sets an HttpOnly cookie at login and hands out a CSRF token for state changes
  const ME = { name: '', role: '', csrf: '' };

  async function api(path, opts) {
    const o = Object.assign({ cache: 'no-store', credentials: 'same-origin' }, opts || {});
    if (o.method && o.method !== 'GET') o.headers = Object.assign({ 'X-CSRF-Token': ME.csrf }, o.headers || {});
    const res = await fetch(path, o);
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (res.status === 401 && path !== '/api/login' && path !== '/api/me') showLogin();
    return { ok: res.ok, status: res.status, body };
  }

  function setMe(me) {
    ME.name = me.name; ME.role = me.role; ME.csrf = me.csrf || '';
    const label = { viewer: '조회', operator: '운영', supervisor: '감독' }[me.role] || me.role;
    $('account').textContent = me.auth === false ? '인증 꺼짐(개발용)' : `${me.name} · ${label}`;
    document.body.classList.toggle('role-viewer', me.role === 'viewer');
    document.body.classList.toggle('role-supervisor', me.role === 'supervisor');
  }

  function showLogin(msg) {
    $('login-veil').hidden = false;
    $('login-error').textContent = msg || '';
    setTimeout(() => $('login-name').focus(), 0);
  }

  async function ensureSession() {
    const r = await api('/api/me');
    if (r.ok && r.body && r.body.ok) { setMe(r.body); return true; }
    showLogin();
    return false;
  }

  async function command(cmd, args, confirmed) {
    const payload = Object.assign({ cmd }, args || {});
    if (confirmed) payload.confirm = true;
    const r = await api('/api/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const msg = r.body && r.body.msg ? r.body.msg : `HTTP ${r.status}`;
    toast(msg, !(r.body && r.body.ok));
    return r.body && r.body.ok;
  }

  function run(cmd, args) {
    if (CONFIRM.has(cmd)) {
      const needReason = cmd === 'fleet_estop';
      ask(`${CMD_LABEL[cmd] || cmd} 실행`, confirmText(cmd, args), needReason, (reason) => command(cmd, Object.assign({}, args, needReason ? { reason } : {}), true));
      return;
    }
    command(cmd, args);
  }

  function confirmText(cmd, args) {
    return ({
      fleet_estop: '모든 로봇에 원격 정지를 보냅니다. 해제해도 자동으로 다시 움직이지 않습니다.',
      fleet_estop_release: '일괄 정지를 해제하고 전체 일시정지 상태로 둡니다. 재개는 따로 합니다.',
      fleet_hold: '모든 로봇을 일시정지합니다.', fleet_resume: '일시정지한 로봇을 모두 재개합니다.',
      zone_evacuate: `${args && args.zone} 구역을 봉쇄하고 안의 로봇을 밖으로 이동시킵니다.`,
      zone_lock: `${args && args.zone} 구역의 신규 진입을 막습니다.`,
      set_mode_manual: `${args && args.robot}의 작업을 분리하고 원격 조종으로 전환합니다. 시뮬레이션은 1배속으로 고정됩니다.`,
    })[cmd] || '실행할까요?';
  }

  async function control(action, value, extra) {
    const r = await api('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ action, value }, extra || {})) });
    if (!r.ok) toast((r.body && r.body.msg) || '실패', true);
    return r.ok;
  }

  // the account the server signed in; the audit log takes the name from the session, not from the page
  function operator() {
    return ME.name || 'operator';
  }

  function connect() {
    if (S.es) S.es.close();
    const es = new EventSource('/api/stream');
    S.es = es;
    es.addEventListener('logout', () => { es.close(); showLogin('세션이 끝났습니다. 다시 로그인하세요'); });
    es.addEventListener('snapshot', (ev) => {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (e) { return; }
      S.connected = true;
      if (snap.layout_key !== S.layoutKey) loadLayout(snap.layout_key);
      S.prev = S.snap;
      S.snap = snap;
      S.snapAt = performance.now();
      renderAll();
    });
    es.onerror = () => {
      S.connected = false;
      $('mode').textContent = '서버 연결 끊김 · 재접속 중';
      $('mode').className = 'val danger';
      $('ribbon').className = 'ribbon estop';
      ensureSession();          // a 401 behind the stream shows the login form
    };
  }

  async function loadLayout(key) {
    S.layoutKey = key;
    const r = await api('/api/layout');
    if (r.ok) {
      S.layout = r.body;
      S.nodes = Object.fromEntries(S.layout.nodes.map((n) => [n.id, n]));
      fillStaticSelects();
    }
  }

  // ------------------------------------------------------------------ toast & confirm
  let toastTimer = null;
  function toast(msg, err) {
    const t = $('toast');
    t.textContent = msg;
    t.className = `toast show${err ? ' err' : ''}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = 'toast'; }, 3200);
  }

  let confirmCb = null;
  function ask(title, text, withInput, cb) {
    $('cf-title').textContent = title;
    $('cf-text').textContent = text;
    $('cf-input').hidden = !withInput;
    $('cf-input').value = '';
    confirmCb = cb;
    $('confirm').hidden = false;
    (withInput ? $('cf-input') : $('cf-ok')).focus();
  }
  $('cf-cancel').onclick = () => { $('confirm').hidden = true; confirmCb = null; };
  $('cf-ok').onclick = () => {
    const value = $('cf-input').value.trim();
    if (!$('cf-input').hidden && !value) { toast('사유를 입력하세요', true); $('cf-input').focus(); return; }   // e.g. fleet e-stop
    const cb = confirmCb; $('confirm').hidden = true; confirmCb = null; if (cb) cb(value);
  };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('confirm').hidden) $('cf-cancel').click(); });

  // ------------------------------------------------------------------ header controls
  async function initMeta() {
    const r = await api('/api/meta');
    if (!r.ok) return;
    S.meta = r.body;
    $('scenario').innerHTML = Object.entries(S.meta.scenarios).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v.description)}">${esc(v.title)}</option>`).join('');
    $('strategy').innerHTML = S.meta.strategies.map((s) => `<option value="${esc(s)}">${esc({ 'locks+resolve': '예약+교착해소', locks: '예약만', none: '제어 없음(안전필드만)' }[s] || s)}</option>`).join('');
    $('speed').innerHTML = S.meta.speeds.map((v) => `<button type="button" data-v="${v}" aria-pressed="false">${v}×</button>`).join('');
    $('speed').onclick = (e) => { const b = e.target.closest('button'); if (b) control('speed', Number(b.dataset.v)); };
    $('scenario').onchange = () => ask('시나리오 전환', `${$('scenario').selectedOptions[0].textContent}로 다시 시작합니다. 현재 상태는 사라집니다.`, false,
      () => control('reset', null, { scenario: $('scenario').value, strategy: $('strategy').value, seed: Number($('rs-seed').value) || 7 }));
    $('strategy').onchange = () => ask('교통 전략 전환', '전략을 바꾸면 같은 시나리오로 다시 시작합니다.', false,
      () => control('reset', null, { scenario: $('scenario').value, strategy: $('strategy').value, seed: Number($('rs-seed').value) || 7 }));
  }

  $('pause-sim').onclick = () => control('pause', !(S.snap && S.snap.run && S.snap.run.paused));
  $('login-form').onsubmit = async (ev) => {
    ev.preventDefault();
    const r = await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: $('login-name').value.trim(), password: $('login-pass').value }) });
    if (!r.ok) { $('login-error').textContent = (r.body && r.body.msg) || `로그인 실패 (HTTP ${r.status})`; return; }
    $('login-pass').value = '';
    $('login-veil').hidden = true;
    setMe(r.body);
    start();
  };
  $('logout').onclick = async () => {
    await api('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    ME.csrf = '';
    if (S.es) S.es.close();
    showLogin('로그아웃했습니다');
  };
  $('fleet-hold').onclick = () => run('fleet_hold');
  $('fleet-resume').onclick = () => run(S.snap && S.snap.mode === 'ESTOP' ? 'fleet_estop_release' : 'fleet_resume');
  $('fleet-estop').onclick = () => run('fleet_estop');
  $('ack-all').onclick = () => command('ack_all', { min_priority: 2, by: operator() });

  $('layers').innerHTML = LAYERS.map(([k, label]) => `<button class="chip" type="button" data-k="${k}" aria-pressed="${S.layers[k]}">${label}</button>`).join('');
  $('layers').onclick = (e) => {
    const b = e.target.closest('.chip');
    if (!b) return;
    S.layers[b.dataset.k] = !S.layers[b.dataset.k];
    b.setAttribute('aria-pressed', S.layers[b.dataset.k]);
    store.set('fct.layers', S.layers);
  };

  $('tablist').innerHTML = TABS.map(([k, label]) => `<button type="button" role="tab" data-k="${k}" aria-selected="false" aria-controls="tab-${k}">${label}</button>`).join('');
  $('tablist').onclick = (e) => { const b = e.target.closest('button'); if (b) selectTab(b.dataset.k); };
  function selectTab(k) {
    S.tab = k;
    store.set('fct.tab', k);
    for (const [key] of TABS) {
      $(`tab-${key}`).hidden = key !== k;
      const b = $('tablist').querySelector(`[data-k="${key}"]`);
      if (b) b.setAttribute('aria-selected', key === k);
    }
    if (k === 'trend') loadHistory();
    if (k === 'vda') loadVda();
    renderTabs();
  }

  // ------------------------------------------------------------------ render: ribbon & KPIs
  function renderAll() {
    const s = S.snap;
    if (!s) return;
    $('clock').textContent = L.fmtClock(s.t);
    const dims = S.layout ? `${S.layout.width}×${S.layout.height} m` : '';
    $('subtitle').textContent = `${s.scenario_title} · ${s.site === 'dc' ? '팔레트 물류센터' : '격자 창고'} ${dims} · 전략 ${({ 'locks+resolve': '예약+교착해소', locks: '예약만', none: '제어 없음' })[s.strategy] || s.strategy} · 시드 ${s.seed}`;
    if ($('scenario').value !== s.scenario) $('scenario').value = s.scenario;
    if ($('strategy').value !== s.strategy) $('strategy').value = s.strategy;
    $('speed').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', Number(b.dataset.v) === s.run.speed));
    $('pause-sim').textContent = s.run.paused ? '재생' : '일시정지';
    $('sim-tag').textContent = s.run.effective_speed < s.run.speed ? '시뮬레이션 · 원격 조종 1×' : '시뮬레이션';
    if (s.run.read_only) document.querySelectorAll('.fleet-actions button, #ack-all').forEach((b) => { b.disabled = true; });
    renderRibbon(s);
    renderKpis(s);
    renderAlarms(s);
    renderUnit(s);
    renderTabs();
  }

  function renderRibbon(s) {
    const mode = { RUNNING: ['정상 운영', 'ok', ''], HOLD: ['전체 일시정지', 'warn', 'hold'], ESTOP: ['일괄 정지(비안전)', 'danger', 'estop'] }[s.mode] || [s.mode, '', ''];
    $('mode').textContent = mode[0] + (s.paused.length && s.mode === 'RUNNING' ? ` · 개별 정지 ${s.paused.length}대` : '');
    $('mode').className = `val ${mode[1]}`;
    $('ribbon').className = `ribbon ${mode[2]}`;
    $('fleet-resume').textContent = s.mode === 'ESTOP' ? '일괄 정지 해제' : '전체 재개';
    const c = s.alarm_counts;
    $('alarm-sum').textContent = `${c.p1} · ${c.p2} · ${c.p3} · 미확인 ${c.unack}`;
    $('alarm-sum').className = `val ${c.p1 ? 'danger' : c.p2 ? 'warn' : ''}`;
    const waiting = s.robots.filter((r) => r.st === 'WAITING').length;
    const blocked = s.robots.filter((r) => r.st === 'BLOCKED' || r.st === 'PROTECTIVE').length;
    $('traffic-sum').textContent = `${waiting} · ${blocked} · 교착 ${s.deadlocks.length}`;
    $('traffic-sum').className = `val ${s.deadlocks.length ? 'danger' : blocked > 3 ? 'warn' : ''}`;
    const humans = s.truth.humans.length;
    const trackedH = s.tracks.filter((t) => t[1] === 'human').length;
    const blind = s.truth.humans.filter((h) => !h[4]).length;
    $('people-sum').textContent = `사람 ${trackedH}/${humans}` + (blind ? ` · 미추적 ${blind}` : '') + ` · 지게차 ${s.tracks.filter((t) => t[1] === 'forklift').length}/${s.truth.forklifts.length}`;
    $('people-sum').className = `val ${blind ? 'warn' : ''}`;
    const jd = s.jdamr;
    if (!jd || jd.enabled === false) { $('jd-sum').textContent = '연동 꺼짐'; $('jd-sum').className = 'val'; }
    else if (jd.connected) { const d = jd.data || {}; $('jd-sum').textContent = `연결 · ${d.drive || '상태 미수신'}${d.blockers && d.blockers.length ? ' · 차단' : ''}`; $('jd-sum').className = `val ${d.blockers && d.blockers.length ? 'warn' : 'ok'}`; }
    else { $('jd-sum').textContent = '미연결'; $('jd-sum').className = 'val'; $('jd-sum').title = jd.error || ''; }
  }

  function renderKpis(s) {
    const k = s.kpi;
    const load = L.alarmLoad(s.alarm_counts.last10min);
    const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
    const site = k.site || {};
    const dc = s.site === 'dc';
    const head = dc ? [
      ['적치·출고/시간', `${L.fmtNum(site.putaway_h, 0)} · ${L.fmtNum(site.shipped_h, 0)}`, `팔레트 · 레인 입고 ${site.staged_in}/출고 ${site.staged_out}`, site.staged_in >= 20 ? 'warn' : ''],
      ['토트 오더/시간', L.fmtNum(site.tote_h, 0), `피킹 ${site.picks}건 · 대기열 ${k.queued}`, ''],
      ['입고→적치', site.dock_to_stock_avg == null ? '—' : L.fmtDuration(site.dock_to_stock_avg), `p90 ${site.dock_to_stock_p90 == null ? '—' : L.fmtDuration(site.dock_to_stock_p90)}`, site.dock_to_stock_avg > 900 ? 'warn' : ''],
    ] : [
      ['처리량/시간', L.fmtNum(k.throughput_h, 0), `완료 ${k.done} · 대기열 ${k.queued}`, ''],
      ['평균 사이클', k.cycle_avg == null ? '—' : L.fmtDuration(k.cycle_avg), `p90 ${k.cycle_p90 == null ? '—' : L.fmtDuration(k.cycle_p90)}`, ''],
    ];
    const cells = [
      ...head,
      ['가동률', pct(k.utilization), `작업 중 ${k.working}/${k.robots}대`, ''],
      ['교통 대기 비율', pct(k.traffic_wait_ratio), '작업 시간 중 대기', k.traffic_wait_ratio > 0.5 ? 'warn' : ''],
      ['교착', `${k.deadlocks}`, `해소 ${k.deadlocks_resolved} · 활성 ${k.deadlocks_active}`, k.deadlocks_active ? 'danger' : ''],
      ...(dc ? [] : [['우회·비켜서기', `${k.reroutes + k.evasions}`, `우회 ${k.reroutes} · 비켜서기 ${k.evasions}`, '']]),
      ['보호정지', `${k.protective_stops}`, `사람 원인 ${k.protective_human}`, ''],
      ['근접사고', `${k.near_miss_truth}`, `관제 인지 ${k.near_miss_fms}`, k.near_miss_truth > 0 ? 'warn' : ''],
      ['주행 중 접촉', `${k.contacts}`, `사람 ${k.contacts_human} · 정지 중 ${k.contacts_static}`, k.contacts > 0 ? 'danger' : ''],
      ['사람 추적률', pct(k.tracking_ratio), '실제 대비(사각지대 포함)', k.tracking_ratio != null && k.tracking_ratio < 0.8 ? 'warn' : ''],
      ['알람/시간', L.fmtNum(k.alarms_per_h, 0), `최근 10분 ${s.alarm_counts.last10min} · ${load.label}`, load.tone === 'danger' ? 'danger' : load.tone === 'warn' ? 'warn' : ''],
      ['MTTR', k.mttr_s == null ? '—' : L.fmtDuration(k.mttr_s), `개입 ${s.run.interventions}회`, ''],
    ];
    $('kpis').innerHTML = cells.map(([label, v, sub, tone]) => `<div class="kpi ${tone}" title="${esc(label)}"><small>${esc(label)}</small><strong>${esc(v)}</strong><small>${esc(sub)}</small></div>`).join('');
  }

  // ------------------------------------------------------------------ alarms
  function renderAlarms(s) {
    const list = s.alarms;
    $('alarm-meta').textContent = `활성 ${list.length}건`;
    $('flood').hidden = !s.alarms.some((a) => a.type === 'ALARM_FLOOD' && a.active);
    const load = L.alarmLoad(s.alarm_counts.last10min);
    $('alarm-load').textContent = `최근 10분 신규 ${s.alarm_counts.last10min}건 · ${load.label} (ISA-18.2 기준)`;
    if (!list.length) { $('alarm-list').innerHTML = '<div class="empty">활성 알람 없음</div>'; return; }
    const html = list.map((a) => {
      const open = S.openAlarm === a.key;
      const cls = `alarm${open ? ' open' : ''}${a.state === 'UNACK' || a.state === 'RTN_UNACK' ? ' unack' : ''}${a.state === 'SHELVED' ? ' shelved' : ''}`;
      const age = L.fmtDuration(s.t - a.raised_at);
      let body = '';
      if (open) {
        const checks = S.checked[a.key] || {};
        const cmds = a.commands.map((c) => `<button class="btn sm" type="button" data-cmd="${esc(c)}" data-key="${esc(a.key)}">${esc(CMD_LABEL[c] || c)}</button>`).join('');
        body = `<div class="sop"><h4>자동 조치</h4><div>${esc(a.auto)}</div><h4>대응 절차 · 응답 ${esc(a.response)}</h4><ol>${a.steps.map((st, i) => `<li><label><input type="checkbox" data-key="${esc(a.key)}" data-i="${i}" ${checks[i] ? 'checked' : ''}> ${esc(st)}</label></li>`).join('')}</ol>
          <div class="row">${cmds}</div>
          <div class="row"><button class="btn primary sm" type="button" data-ack="${esc(a.key)}" ${a.state === 'ACKED' ? 'disabled' : ''}>확인(ACK)</button>
          ${a.priority > 1 ? `<select class="ctl" data-shelve-min="${esc(a.key)}" style="height:26px"><option value="10">10분</option><option value="30">30분</option><option value="60">60분</option></select><input class="ctl" data-shelve-reason="${esc(a.key)}" placeholder="보류 사유" style="height:26px;width:120px"><button class="btn sm" type="button" data-shelve="${esc(a.key)}">보류</button>` : '<span class="meta">긴급 알람은 보류할 수 없음</span>'}</div>
          ${a.shelve_reason ? `<div class="meta" style="text-align:left;margin-top:5px">보류: ${esc(a.shelve_reason)}</div>` : ''}</div>`;
      }
      return `<div class="${cls}"><button type="button" data-open="${esc(a.key)}"><span class="prio p${a.priority}">${esc(a.priority_label)}</span><span><div class="t">${esc(a.title)} · ${esc(a.source)}</div><div class="m">${esc(a.message)}</div></span><span class="st">${esc(L.alarmStateLabel(a.state))}<br>${esc(age)}${a.count > 1 ? ` · ${a.count}회` : ''}</span></button>${body}</div>`;
    }).join('');
    const scroll = $('alarm-list').scrollTop;
    const focusKey = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.shelveReason : null;
    if (focusKey) return;   // do not re-render while the operator types a shelve reason
    $('alarm-list').innerHTML = html;
    $('alarm-list').scrollTop = scroll;
  }

  $('alarm-list').addEventListener('click', (e) => {
    const t = e.target;
    const openBtn = t.closest('[data-open]');
    if (openBtn && !t.closest('.sop')) {
      const key = openBtn.dataset.open;
      S.openAlarm = S.openAlarm === key ? null : key;
      const a = S.snap && S.snap.alarms.find((x) => x.key === key);
      if (a && S.snap.robots.some((r) => r.id === a.source)) S.sel = { kind: 'robot', id: a.source };
      renderAll();
      return;
    }
    if (t.dataset.ack) { command('ack_alarm', { key: t.dataset.ack, by: operator() }); return; }
    if (t.dataset.shelve) {
      const key = t.dataset.shelve;
      const minutes = Number(document.querySelector(`[data-shelve-min="${CSS.escape(key)}"]`).value);
      const reason = document.querySelector(`[data-shelve-reason="${CSS.escape(key)}"]`).value;
      command('shelve_alarm', { key, minutes, reason, by: operator() });
      return;
    }
    if (t.dataset.cmd) sopCommand(t.dataset.cmd, t.dataset.key);
  });
  $('alarm-list').addEventListener('change', (e) => {
    const t = e.target;
    if (t.type === 'checkbox' && t.dataset.key) {
      S.checked[t.dataset.key] = S.checked[t.dataset.key] || {};
      S.checked[t.dataset.key][t.dataset.i] = t.checked;
    }
  });

  function sopCommand(cmd, key) {
    const a = S.snap.alarms.find((x) => x.key === key);
    const src = a ? a.source : '';
    const robot = S.snap.robots.find((r) => r.id === src);
    if (cmd === 'zone_speed') { selectTab('zones'); return; }
    if (cmd.startsWith('door_')) { command(cmd, { door: src }); return; }
    if (cmd === 'set_task_rate' || cmd === 'reprioritize_task') { selectTab('tasks'); return; }
    if (cmd === 'zone_lock') {
      const p = a && a.data && a.data.x != null ? [a.data.x, a.data.y] : robot ? [robot.x, robot.y] : null;
      const z = p && zoneAt(p[0], p[1]);
      if (!z) { toast('구역을 특정할 수 없습니다: 구역 탭에서 선택하세요', true); selectTab('zones'); return; }
      run('zone_lock', { zone: z.id });
      return;
    }
    if (cmd === 'zone_unlock') {
      const zid = robot && robot.wait_holder;
      if (!zid) { selectTab('zones'); return; }
      run('zone_unlock', { zone: zid });
      return;
    }
    if (cmd === 'cancel_task') {
      const members = (a && a.data && a.data.members) || [src];
      const r = S.snap.robots.find((x) => members.includes(x.id) && x.task && !x.load) || robot;
      if (!r || !r.task) { toast('취소할 작업이 없습니다', true); return; }
      run('cancel_task', { task: r.task });
      return;
    }
    if (['resume_area', 'fleet_estop_release', 'fleet_resume', 'fleet_hold'].includes(cmd)) { run(cmd); return; }
    if (!robot) { toast('로봇 대상 명령입니다', true); return; }
    run(cmd, { robot: robot.id });
  }

  function zoneAt(x, y) {
    if (!S.layout) return null;
    const hits = S.layout.zones.filter((z) => pointInPoly(x, y, z.polygon));
    return hits.find((z) => z.kind !== 'aisle') || hits[0] || null;
  }

  function pointInPoly(x, y, poly) {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i];
      const [xj, yj] = poly[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // ------------------------------------------------------------------ selected unit
  function renderUnit(s) {
    const body = $('unit-body');
    if (!S.sel) { body.innerHTML = '<div class="empty">지도·목록·알람에서 유닛을 선택하세요</div>'; $('unit-meta').textContent = '—'; stopLocal(); return; }
    if (S.sel.kind !== 'robot') { renderOther(s, body); stopLocal(); return; }
    const r = s.robots.find((x) => x.id === S.sel.id);
    if (!r) { body.innerHTML = '<div class="empty">유닛이 사라졌습니다</div>'; return; }
    const info = L.statusInfo(r.st);
    $('unit-meta').textContent = `주문 ${r.order || '—'} · 갱신 ${r.upd} · 수신 ${r.age}초 전`;
    const manual = r.mode === 'MANUAL';
    const kv = [
      ['배터리 · 주행 가능', `${L.fmtNum(r.bat, 0)}%${r.chg ? ' 충전 중' : ''} · ${r.reach == null ? '—' : `${(r.reach / 1000).toFixed(1)} km`}`], ['속도', `${L.fmtNum(r.v, 2)} m/s`], ['운전 모드', r.mode],
      ['통신(수신율 · 지연)', `${L.linkInfo(r.conn, r.link, r.lat_ms).text} · 마지막 수신 ${L.fmtNum(r.age, 1)}초 전`], ['현재 노드', r.node || '—'], ['목적지', r.goal ? `${r.goal} · ${r.purpose}` : '—'],
      ['작업', r.task || '—'], ['적재', r.load ? '있음' : '없음'], ['예약 자원', `${r.locks}개`],
      ['안전필드', { CLEAR: '정상', WARNING: '경고(감속)', PROTECTIVE: '보호정지' }[r.field] || r.field], ['대기', r.wait_s ? `${L.fmtDuration(r.wait_s)} · ${r.wait_kind}` : '—'], ['차량 보고 사유', r.stop || '—'],
    ];
    const cmds = [];
    const paused = s.paused.includes(r.id);
    cmds.push(paused ? ['resume_robot', '재개'] : ['pause_robot', '일시정지']);
    if (r.fault) cmds.push(['reset_fault', '원격 리셋'], ['field_service_done', '현장 조치 완료']);
    if (r.fault === 'LOCALIZATION_LOST' || !r.positioned) cmds.push(['init_position', '위치 재지정']);
    if (r.task) cmds.push(['reassign_task', '작업 재할당']);
    if (!r.task && !manual) cmds.push(['send_to_park', '주차'], ['send_to_charge', '충전']);
    cmds.push(manual ? ['set_mode_auto', '자동 복귀'] : ['set_mode_manual', '원격 조종 인계']);
    const teleop = manual ? `<div class="teleop"><canvas id="local" aria-label="로봇 중심 근접 뷰"></canvas><div><div class="pad" id="pad">
      <span></span><button type="button" data-d="f" aria-label="전진">▲</button><span></span>
      <button type="button" data-d="l" aria-label="좌회전">◀</button><button type="button" data-d="s" aria-label="정지">■</button><button type="button" data-d="r" aria-label="우회전">▶</button>
      <span></span><button type="button" data-d="b" aria-label="후진">▼</button><span></span></div><div class="meta" style="text-align:center;margin-top:6px">누르고 있는 동안만 이동 · 방향키 가능<br>0.6초 무입력 시 정지(데드맨)</div></div></div>` : '';
    body.innerHTML = `<div class="unit-head"><strong>${esc(r.id)}</strong><span class="tag ${info.tone}">${esc(r.label)}</span>${r.fault ? `<span class="tag danger">${esc(r.fault)}</span>` : ''}</div>
      <div class="why">${esc(r.why || '정상')}</div>
      <div class="kv">${kv.map(([k, v]) => `<div><span class="kicker">${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div>
      <div class="cmds">${cmds.map(([c, label]) => `<button class="btn sm${c === 'set_mode_manual' ? ' warn' : ''}" type="button" data-rcmd="${c}">${label}</button>`).join('')}</div>${teleop}
      <div class="note">관제 화면은 버스로 받은 상태만 보여 줍니다(지연·손실 포함). 실제 위치는 지도 레이어 "실제 위치(평가)"로 비교합니다.</div>`;
    if (manual) startLocal(r.id); else stopLocal();
  }

  $('unit-body').addEventListener('click', (e) => {
    const b = e.target.closest('[data-rcmd]');
    if (!b || !S.sel) return;
    run(b.dataset.rcmd, { robot: S.sel.id });
  });

  function renderOther(s, body) {
    const id = S.sel.id;
    let html = '';
    if (S.sel.kind === 'track') {
      const t = s.tracks.find((x) => x[0] === id);
      if (!t) { body.innerHTML = '<div class="empty">추적이 끝났습니다(소실·만료)</div>'; return; }
      html = `<div class="unit-head"><strong>${esc(t[0])}</strong><span class="tag warn">${t[1] === 'human' ? '사람' : '지게차'} · 통제 불가</span></div>
        <div class="kv"><div><span class="kicker">위치</span><b>${t[2].toFixed(1)}, ${t[3].toFixed(1)}</b></div><div><span class="kicker">속도</span><b>${Math.hypot(t[4], t[5]).toFixed(2)} m/s</b></div><div><span class="kicker">위치 불확실성</span><b>±${t[6].toFixed(2)} m</b></div><div><span class="kicker">마지막 관측</span><b>${t[7]}초 전</b></div></div>
        <div class="note">관제는 명령할 수 없습니다. 예측 점유 구간의 노드·레인을 예약에서 제외해 로봇이 들어가지 않게 합니다.</div>`;
    } else if (S.sel.kind === 'ext') {
      const x = s.external.find((e) => e.id === id);
      if (!x) { body.innerHTML = '<div class="empty">외부 로봇 신호 없음</div>'; return; }
      html = `<div class="unit-head"><strong>${esc(x.id)}</strong><span class="tag special">외부 로봇 · 읽기 전용</span></div>
        <div class="kv"><div><span class="kicker">위치</span><b>${x.x.toFixed(1)}, ${x.y.toFixed(1)}</b></div><div><span class="kicker">예정 경로</span><b>${esc(x.itinerary.join(' → '))}</b></div></div>
        <div class="note">타사 청소로봇입니다. 어댑터가 알려 준 예정 경로를 예약 테이블에 넣고 우리 로봇이 양보합니다(Open-RMF read-only fleet 방식).</div>`;
    }
    body.innerHTML = html;
    $('unit-meta').textContent = '';
  }

  // teleop: local view + pad
  function startLocal(id) {
    if (S.teleop.localTimer && S.teleop.localId === id) return;
    stopLocal();
    S.teleop.localId = id;
    const tick = async () => {
      const r = await api(`/api/local/${encodeURIComponent(id)}`);
      if (r.ok) { S.teleop.local = r.body; drawLocal(); }
    };
    tick();
    S.teleop.localTimer = setInterval(tick, 250);
  }
  function stopLocal() {
    clearInterval(S.teleop.localTimer);
    S.teleop.localTimer = null;
    S.teleop.localId = null;
  }
  function drawLocal() {
    const c = $('local');
    const d = S.teleop.local;
    if (!c || !d) return;
    const ctx = setupCanvas(c);
    const w = c.clientWidth;
    const h = c.clientHeight;
    const scale = Math.min(w, h) / 12;
    const r = d.robot;
    const tr = (x, y) => {
      const dx = x - r.x;
      const dy = y - r.y;
      const c0 = Math.cos(-r.th + Math.PI / 2);
      const s0 = Math.sin(-r.th + Math.PI / 2);
      return [w / 2 + (dx * c0 - dy * s0) * scale, h / 2 - (dx * s0 + dy * c0) * scale];
    };
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = '#e2e8f0';
    for (const rack of d.racks) { ctx.beginPath(); rack.forEach(([x, y], i) => { const [px, py] = tr(x, y); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }); ctx.closePath(); ctx.fill(); }
    ctx.strokeStyle = '#dce4ee';
    for (const k of [1, 2, 3, 4, 5]) { ctx.beginPath(); ctx.arc(w / 2, h / 2, k * scale, 0, Math.PI * 2); ctx.stroke(); }
    const field = d.field.state;
    ctx.fillStyle = field === 'PROTECTIVE' ? 'rgba(180,35,50,.25)' : field === 'WARNING' ? 'rgba(153,99,19,.18)' : 'rgba(23,97,196,.08)';
    ctx.fillRect(w / 2 - 0.6 * scale, h / 2 - 1.2 * scale, 1.2 * scale, 0.75 * scale);
    for (const o of d.robots) { const [px, py] = tr(o[1], o[2]); ctx.fillStyle = '#64748b'; ctx.fillRect(px - 0.4 * scale, py - 0.4 * scale, 0.8 * scale, 0.8 * scale); }
    for (const t of d.tracks) { const [px, py] = tr(t[2], t[3]); ctx.fillStyle = t[1] === 'human' ? '#0f172a' : '#a16207'; ctx.beginPath(); ctx.arc(px, py, (t[1] === 'human' ? 0.3 : 0.8) * scale, 0, Math.PI * 2); ctx.fill(); }
    for (const t of d.detections) { const [px, py] = tr(t.x, t.y); ctx.strokeStyle = '#c2410c'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(px, py, 0.45 * scale, 0, Math.PI * 2); ctx.stroke(); }
    ctx.fillStyle = '#6d28d9';
    ctx.fillRect(w / 2 - 0.35 * scale, h / 2 - 0.45 * scale, 0.7 * scale, 0.9 * scale);
    ctx.fillStyle = '#fff';
    ctx.fillRect(w / 2 - 0.2 * scale, h / 2 - 0.4 * scale, 0.4 * scale, 0.12 * scale);
    ctx.fillStyle = '#53657a';
    ctx.font = '11px system-ui';
    ctx.fillText(`보호필드 ${ { CLEAR: '정상', WARNING: '경고', PROTECTIVE: '정지' }[field] || field } · 1칸=1 m · 위가 전방`, 6, h - 6);
  }

  const DIRS = { f: [0.4, 0], b: [-0.2, 0], l: [0, 0.8], r: [0, -0.8], s: [0, 0] };
  function sendTeleop() {
    if (!S.sel || !S.snap) return;
    const r = S.snap.robots.find((x) => x.id === S.sel.id);
    if (!r || r.mode !== 'MANUAL') return;
    let vx = 0;
    let om = 0;
    for (const k of S.teleop.keys) { vx += DIRS[k][0]; om += DIRS[k][1]; }
    api('/api/command', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: 'teleop', robot: r.id, vx, omega: om }) });
  }
  function teleopStart(d) {
    S.teleop.keys.add(d);
    sendTeleop();
    if (!S.teleop.timer) S.teleop.timer = setInterval(sendTeleop, 200);
  }
  function teleopStop(d) {
    if (d) S.teleop.keys.delete(d); else S.teleop.keys.clear();
    if (!S.teleop.keys.size) { clearInterval(S.teleop.timer); S.teleop.timer = null; }
  }
  $('unit-body').addEventListener('pointerdown', (e) => { const b = e.target.closest('#pad button'); if (b) { b.classList.add('on'); teleopStart(b.dataset.d); } });
  document.addEventListener('pointerup', () => { document.querySelectorAll('#pad button.on').forEach((b) => b.classList.remove('on')); teleopStop(); });
  const KEYMAP = { ArrowUp: 'f', ArrowDown: 'b', ArrowLeft: 'l', ArrowRight: 'r' };
  document.addEventListener('keydown', (e) => {
    if (!KEYMAP[e.key] || !$('pad') || ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    e.preventDefault();
    if (!S.teleop.keys.has(KEYMAP[e.key])) teleopStart(KEYMAP[e.key]);
  });
  document.addEventListener('keyup', (e) => { if (KEYMAP[e.key]) teleopStop(KEYMAP[e.key]); });
  window.addEventListener('blur', () => teleopStop());

  // ------------------------------------------------------------------ map
  const canvas = $('map');
  let drag = null;

  function setupCanvas(c) {
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = c.clientHeight;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function mapTransform() {
    const base = L.fitTransform(S.layout.width, S.layout.height, canvas.clientWidth, canvas.clientHeight, 14);
    return { base, t: L.applyView(base, S.view) };
  }

  function robotPositions() {
    const s = S.snap;
    const a = Math.min(1, (performance.now() - S.snapAt) / 260);
    const prev = S.prev ? Object.fromEntries(S.prev.robots.map((r) => [r.id, r])) : {};
    return s.robots.map((r) => {
      const p = prev[r.id];
      if (!p || Math.hypot(p.x - r.x, p.y - r.y) > 3) return Object.assign({}, r, { dx: r.x, dy: r.y, dth: r.th });
      return Object.assign({}, r, { dx: L.lerp(p.x, r.x, a), dy: L.lerp(p.y, r.y, a), dth: L.lerpAngle(p.th, r.th, a) });
    });
  }

  function drawMap() {
    requestAnimationFrame(drawMap);
    if (!S.layout || !S.snap) return;
    const ctx = setupCanvas(canvas);
    const { t } = mapTransform();
    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    const s = S.snap;
    const P = (x, y) => L.toScreen(t, x, y);
    const k = t.scale;
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(0, 0, W, H);
    // floor & walls
    const [fx, fy] = P(0, S.layout.height);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(fx, fy, S.layout.width * k, S.layout.height * k);
    ctx.strokeStyle = '#94a3b8';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(fx, fy, S.layout.width * k, S.layout.height * k);
    const poly = (pts) => { ctx.beginPath(); pts.forEach(([x, y], i) => { const [px, py] = P(x, y); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }); ctx.closePath(); };
    // zones
    const zoneDyn = Object.fromEntries(s.zones.map((z) => [z.id, z]));
    if (S.layers.zones) {
      for (const z of S.layout.zones) {
        const dz = zoneDyn[z.id] || {};
        if (z.kind === 'aisle' && !dz.locked && dz.limit == null) continue;
        poly(z.polygon);
        const fill = dz.locked ? 'rgba(180,35,50,.14)' : dz.limit != null ? 'rgba(242,196,107,.22)' : { human_work: 'rgba(15,118,110,.06)', walkway: 'rgba(15,118,110,.05)', picking: 'rgba(15,118,110,.07)', forklift_lane: 'rgba(161,98,7,.06)', charging: 'rgba(22,128,91,.06)' }[z.kind] || 'rgba(100,116,139,.05)';
        ctx.fillStyle = fill;
        ctx.fill();
        if (dz.locked) { ctx.strokeStyle = '#b42332'; ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5; ctx.stroke(); ctx.setLineDash([]); }
        if (k > 9 && (dz.locked || dz.limit != null || z.kind !== 'aisle')) {
          const [lx, ly] = P(z.polygon[0][0] + 0.2, z.polygon[2][1] - 0.2);
          ctx.fillStyle = dz.locked ? '#b42332' : dz.limit != null ? '#7a4f0c' : '#5b7083';
          ctx.font = '10px system-ui';
          ctx.fillText(`${z.name}${dz.locked ? ' · 봉쇄' : dz.limit != null ? ` · ${dz.limit} m/s` : ''}`, lx, ly + 10);
        }
      }
    }
    if (S.layers.cams) {
      ctx.strokeStyle = 'rgba(23,97,196,.45)';
      ctx.setLineDash([3, 3]);
      for (const c of S.layout.coverage) { poly(c.polygon); ctx.stroke(); }
      ctx.setLineDash([]);
    }
    // racks
    ctx.fillStyle = '#e2e8f0';
    ctx.strokeStyle = '#cbd5e1';
    ctx.lineWidth = 1;
    for (const r of S.layout.racks) { poly(r); ctx.fill(); ctx.stroke(); }
    // fire wall and rapid-roll doors: grey while normal, red only when failed or out of use (ISA-101)
    ctx.strokeStyle = '#94a3b8';
    ctx.lineWidth = Math.max(2, k * 0.25);
    for (const w of S.layout.walls || []) {
      const [ax, ay] = P(w[0][0], w[0][1]);
      const [bx, by] = P(w[1][0], w[1][1]);
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    }
    const doorState = Object.fromEntries(((S.snap && S.snap.doors) || []).map((d) => [d.id, d]));
    for (const d of S.layout.doors || []) {
      const st = doorState[d.id] || {};
      const bad = st.cause || st.disabled;
      const [ax, ay] = P(d.a[0], d.a[1]);
      const [bx, by] = P(d.b[0], d.b[1]);
      const open = st.open == null ? 0 : st.open;
      ctx.strokeStyle = bad ? '#b42332' : open >= 1 ? '#cbd5e1' : '#475569';
      ctx.lineWidth = Math.max(2, k * (open >= 1 ? 0.12 : 0.3));
      ctx.setLineDash(open >= 1 ? [4, 3] : []);
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = bad ? '#b42332' : '#64748b';
      ctx.font = `${Math.max(9, k * 0.55)}px system-ui, sans-serif`;
      ctx.fillText(`${d.id}${bad ? (st.disabled ? ' 사용 중지' : ' 이상') : ''}`, ax + 4, (ay + by) / 2);
    }
    // roadmap
    ctx.strokeStyle = '#d6dee8';
    ctx.lineWidth = Math.max(1, k * 0.12);
    for (const e of S.layout.edges) {
      const a = S.nodes[e.s];
      const b = S.nodes[e.e];
      const [ax, ay] = P(a.x, a.y);
      const [bx, by] = P(b.x, b.y);
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      if (e.oneway && k > 11) {
        const mx = (ax + bx) / 2;
        const my = (ay + by) / 2;
        const ang = Math.atan2(by - ay, bx - ax);
        ctx.fillStyle = '#b6c3d2';
        ctx.beginPath(); ctx.moveTo(mx + Math.cos(ang) * 4, my + Math.sin(ang) * 4); ctx.lineTo(mx + Math.cos(ang + 2.5) * 4, my + Math.sin(ang + 2.5) * 4); ctx.lineTo(mx + Math.cos(ang - 2.5) * 4, my + Math.sin(ang - 2.5) * 4); ctx.fill();
      }
    }
    // congestion heat
    if (S.layers.heat) {
      const max = Math.max(1, ...Object.values(s.heat));
      for (const [n, v] of Object.entries(s.heat)) {
        const node = S.nodes[n];
        if (!node) continue;
        const [px, py] = P(node.x, node.y);
        ctx.fillStyle = `rgba(180,35,50,${0.12 + 0.45 * (v / max)})`;
        ctx.beginPath(); ctx.arc(px, py, (0.5 + 1.3 * (v / max)) * k, 0, Math.PI * 2); ctx.fill();
      }
    }
    // site state: pallets waiting on the floor staging lanes, rack slot occupancy, trucks at doors
    const st = s.site_state || {};
    if (st.rack && k > 5) {
      for (const [n, cnt] of Object.entries(st.rack)) {
        const node = S.nodes[n];
        if (!node) continue;
        const [px, py] = P(node.x, node.y);
        const frac = cnt / (st.slots_per_node || 6);
        ctx.fillStyle = `rgba(146,110,52,${0.12 + 0.45 * frac})`;
        ctx.fillRect(px - 2.95 * k, py - 1.4 * k, 0.9 * k, 2.8 * k);
        ctx.fillRect(px + 2.05 * k, py - 1.4 * k, 0.9 * k, 2.8 * k);
      }
    }
    const lane = (map, dir) => {
      for (const [n, cnt] of Object.entries(map || {})) {
        const node = S.nodes[n];
        if (!node || !cnt) continue;
        for (let i = 0; i < cnt; i++) {
          const [px, py] = P(node.x + dir * (1.55 + i * 1.2), node.y);
          ctx.fillStyle = '#d6b37a'; ctx.strokeStyle = '#8a6a2f'; ctx.lineWidth = 1;
          ctx.fillRect(px - 0.5 * k, py - 0.5 * k, 1.0 * k, 1.0 * k); ctx.strokeRect(px - 0.5 * k, py - 0.5 * k, 1.0 * k, 1.0 * k);
        }
        if (k > 7) { const [lx, ly] = P(node.x + dir * 1.55, node.y + 1.1); ctx.fillStyle = '#7a5a20'; ctx.font = '600 10px system-ui'; ctx.fillText(`${cnt}/${st.lane_capacity || 4}`, lx - 8, ly); }
      }
    };
    lane(st.in_stage, -1);
    lane(st.out_stage, 1);
    if (st.docks_in && k > 6) {
      for (const [dock, left] of Object.entries(st.docks_in)) {
        const node = S.nodes[`SI${dock.slice(2)}`];
        if (!node) continue;
        const [px, py] = P(0.2, node.y + 1.6);
        ctx.fillStyle = '#334155'; ctx.font = '600 10px system-ui'; ctx.fillText(`트럭 ${left}`, px, py);
      }
    }
    // stations
    for (const n of S.layout.nodes) {
      const [px, py] = P(n.x, n.y);
      if (['inbound', 'outbound', 'charger', 'park', 'in_stage', 'out_stage', 'pack'].includes(n.kind)) {
        ctx.fillStyle = { inbound: '#dbeafe', outbound: '#dcfce7', charger: '#fef3c7', park: '#f1f5f9', in_stage: '#dbeafe', out_stage: '#dcfce7', pack: '#ede9fe' }[n.kind];
        ctx.strokeStyle = '#94a3b8';
        const sz = 0.7 * k;
        ctx.fillRect(px - sz / 2, py - sz / 2, sz, sz);
        ctx.strokeRect(px - sz / 2, py - sz / 2, sz, sz);
        if (k > 10 && n.kind !== 'park') { ctx.fillStyle = '#53657a'; ctx.font = '9px system-ui'; ctx.fillText(n.id, px - sz / 2, py - sz / 2 - 2); }
      } else if (n.intersection) {
        ctx.fillStyle = '#c3cfdc';
        ctx.fillRect(px - 1.5, py - 1.5, 3, 3);
      }
    }
    // predicted occupancy blocks
    if (S.layers.predict) {
      for (const [res, [kind]] of Object.entries(s.blocks)) {
        if (kind === 'zone') continue;
        ctx.strokeStyle = kind === 'forklift' ? 'rgba(161,98,7,.45)' : 'rgba(194,65,12,.4)';
        ctx.fillStyle = ctx.strokeStyle;
        if (res.startsWith('N:')) {
          const node = S.nodes[res.slice(2)];
          if (!node) continue;
          const [px, py] = P(node.x, node.y);
          ctx.beginPath(); ctx.arc(px, py, 0.55 * k, 0, Math.PI * 2); ctx.fill();
        } else {
          const [a, b] = res.slice(2).split('|');
          if (!S.nodes[a] || !S.nodes[b]) continue;
          const [ax, ay] = P(S.nodes[a].x, S.nodes[a].y);
          const [bx, by] = P(S.nodes[b].x, S.nodes[b].y);
          ctx.lineWidth = Math.max(3, 0.5 * k);
          ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        }
      }
    }
    const robots = robotPositions();
    const color = (r) => L.statusInfo(r.st).color;
    // reservations
    if (S.layers.locks) {
      ctx.lineWidth = Math.max(3, 0.45 * k);
      for (const [lane, owner] of Object.entries(s.lanes)) {
        const [a, b] = lane.split('|');
        if (!S.nodes[a] || !S.nodes[b]) continue;
        const [ax, ay] = P(S.nodes[a].x, S.nodes[a].y);
        const [bx, by] = P(S.nodes[b].x, S.nodes[b].y);
        ctx.strokeStyle = owner.startsWith('EXT') ? 'rgba(14,116,144,.25)' : owner === (S.sel && S.sel.id) ? 'rgba(23,97,196,.45)' : 'rgba(23,97,196,.14)';
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      }
      for (const [n, owner] of Object.entries(s.locks)) {
        const node = S.nodes[n];
        if (!node) continue;
        const [px, py] = P(node.x, node.y);
        const sz = 0.55 * k;
        ctx.fillStyle = owner.startsWith('EXT') ? 'rgba(14,116,144,.3)' : owner === (S.sel && S.sel.id) ? 'rgba(23,97,196,.5)' : 'rgba(23,97,196,.2)';
        ctx.fillRect(px - sz / 2, py - sz / 2, sz, sz);
      }
    }
    // routes: base solid, horizon dashed
    if (S.layers.routes) {
      for (const r of robots) {
        const selected = S.sel && S.sel.kind === 'robot' && S.sel.id === r.id;
        if (!r.route.length || (!selected && r.route.length < 2)) continue;
        const drawChain = (ids, from) => {
          ctx.beginPath();
          let [px, py] = from;
          ctx.moveTo(px, py);
          for (const id of ids) { const n = S.nodes[id]; if (!n) continue; [px, py] = P(n.x, n.y); ctx.lineTo(px, py); }
          ctx.stroke();
        };
        const start = P(r.dx, r.dy);
        ctx.lineWidth = selected ? 3 : 1.5;
        ctx.strokeStyle = selected ? '#1761c4' : 'rgba(51,65,85,.45)';
        drawChain(r.base.slice(1), start);
        const baseEnd = r.base.length ? S.nodes[r.base[r.base.length - 1]] : null;
        if (baseEnd && r.hz.length) {
          ctx.setLineDash([5, 4]);
          ctx.lineWidth = selected ? 2 : 1;
          ctx.strokeStyle = selected ? 'rgba(23,97,196,.6)' : 'rgba(100,116,139,.35)';
          drawChain(r.hz, P(baseEnd.x, baseEnd.y));
          ctx.setLineDash([]);
        }
        if (selected && r.goal && S.nodes[r.goal]) {
          const [gx, gy] = P(S.nodes[r.goal].x, S.nodes[r.goal].y);
          ctx.strokeStyle = '#1761c4'; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(gx, gy, 0.7 * k, 0, Math.PI * 2); ctx.stroke();
        }
      }
    }
    // wait-for relations and deadlock cycles
    if (S.layers.wait) {
      const byId = Object.fromEntries(robots.map((r) => [r.id, r]));
      const inCycle = new Set(s.deadlocks.flatMap((d) => d.members));
      for (const r of robots) {
        if (!r.wait_holder || !byId[r.wait_holder]) continue;
        const h = byId[r.wait_holder];
        const [ax, ay] = P(r.dx, r.dy);
        const [bx, by] = P(h.dx, h.dy);
        const cyc = inCycle.has(r.id) && inCycle.has(h.id);
        ctx.strokeStyle = cyc ? '#b42332' : 'rgba(161,98,7,.7)';
        ctx.lineWidth = cyc ? 2.5 : 1.2;
        ctx.setLineDash(cyc ? [] : [3, 3]);
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        ctx.setLineDash([]);
        const ang = Math.atan2(by - ay, bx - ax);
        const hx = bx - Math.cos(ang) * 0.6 * k;
        const hy = by - Math.sin(ang) * 0.6 * k;
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath(); ctx.moveTo(hx, hy); ctx.lineTo(hx - Math.cos(ang - 0.5) * 7, hy - Math.sin(ang - 0.5) * 7); ctx.lineTo(hx - Math.cos(ang + 0.5) * 7, hy - Math.sin(ang + 0.5) * 7); ctx.fill();
      }
    }
    // external robots
    for (const x of s.external) {
      const [px, py] = P(x.x, x.y);
      if (S.layers.routes) {
        ctx.strokeStyle = 'rgba(14,116,144,.5)'; ctx.setLineDash([2, 3]); ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(px, py);
        for (const id of x.itinerary.slice(1)) { const n = S.nodes[id]; if (n) { const [qx, qy] = P(n.x, n.y); ctx.lineTo(qx, qy); } }
        ctx.stroke(); ctx.setLineDash([]);
      }
      const r0 = 0.55 * k;
      ctx.fillStyle = '#0e7490';
      ctx.beginPath(); ctx.moveTo(px, py - r0); ctx.lineTo(px + r0, py); ctx.lineTo(px, py + r0); ctx.lineTo(px - r0, py); ctx.closePath(); ctx.fill();
      if (S.sel && S.sel.kind === 'ext' && S.sel.id === x.id) { ctx.strokeStyle = '#1761c4'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(px, py, r0 + 5, 0, Math.PI * 2); ctx.stroke(); }
      if (S.layers.ids && k > 8) { ctx.fillStyle = '#0e7490'; ctx.font = '10px system-ui'; ctx.fillText(x.id, px + r0 + 2, py - 2); }
    }
    // tracks (what the control room believes) with prediction
    for (const tr of s.tracks) {
      const [id, cls, x, y, vx, vy, sig] = tr;
      const [px, py] = P(x, y);
      if (S.layers.predict && Math.hypot(vx, vy) >= 0.3) {
        const horizon = cls === 'human' ? 2 : 4;
        const [qx, qy] = P(x + vx * horizon, y + vy * horizon);
        ctx.strokeStyle = cls === 'human' ? 'rgba(194,65,12,.6)' : 'rgba(161,98,7,.6)';
        ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(qx, qy); ctx.stroke();
      }
      if (S.layers.predict) {
        ctx.fillStyle = 'rgba(194,65,12,.08)';
        ctx.beginPath(); ctx.arc(px, py, (cls === 'human' ? 0.65 : 1.55) * k + sig * k, 0, Math.PI * 2); ctx.fill();
      }
      if (cls === 'human') {
        ctx.fillStyle = '#0f172a';
        ctx.beginPath(); ctx.arc(px, py, Math.max(3, 0.3 * k), 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
      } else {
        const ang = Math.hypot(vx, vy) > 0.1 ? Math.atan2(vy, vx) : 0;
        ctx.save(); ctx.translate(px, py); ctx.rotate(-ang);
        ctx.fillStyle = '#a16207'; ctx.fillRect(-1.8 * k, -0.58 * k, 2.4 * k, 1.17 * k);
        ctx.fillStyle = '#ca8a04'; ctx.fillRect(0.6 * k, -0.42 * k, 1.2 * k, 0.16 * k); ctx.fillRect(0.6 * k, 0.26 * k, 1.2 * k, 0.16 * k);
        ctx.restore();
      }
      if (S.sel && S.sel.kind === 'track' && S.sel.id === id) { ctx.strokeStyle = '#1761c4'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(px, py, 0.9 * k, 0, Math.PI * 2); ctx.stroke(); }
    }
    // evaluation layer: ground truth
    if (S.layers.truth) {
      for (const h of s.truth.humans) {
        const [px, py] = P(h[1], h[2]);
        ctx.strokeStyle = h[4] ? '#16805b' : '#b42332';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(px, py, Math.max(4, 0.35 * k), 0, Math.PI * 2); ctx.stroke();
        if (k > 8) { ctx.fillStyle = h[4] ? '#16805b' : '#b42332'; ctx.font = '9px system-ui'; ctx.fillText(`${h[0]}${h[4] ? '' : ' 미추적'}`, px + 5, py + 12); }
      }
      for (const f of s.truth.forklifts) {
        const [px, py] = P(f[1], f[2]);
        ctx.save(); ctx.translate(px, py); ctx.rotate(-f[3]);
        ctx.strokeStyle = '#16805b'; ctx.lineWidth = 1.5; ctx.strokeRect(-1.8 * k, -0.58 * k, 3.6 * k, 1.17 * k);
        ctx.restore();
      }
    }
    // robots
    for (const r of robots) {
      const [px, py] = P(r.dx, r.dy);
      ctx.save(); ctx.translate(px, py); ctx.rotate(-r.dth);
      const Lr = (r.len || 0.9) * k;
      const Wr = (r.wid || 0.7) * k;
      const agf = r.cls === 'FORKLIFT';
      ctx.fillStyle = color(r);
      if (agf) {
        // counterbalance chassis (rear 60 %) and two fork tines in front
        const body = Lr * 0.6;
        ctx.fillRect(-Lr / 2, -Wr / 2, body, Wr);
        ctx.fillRect(-Lr / 2 + body, -Wr * 0.36, Lr * 0.4, Wr * 0.14);
        ctx.fillRect(-Lr / 2 + body, Wr * 0.22, Lr * 0.4, Wr * 0.14);
        ctx.fillStyle = 'rgba(255,255,255,.8)';
        ctx.fillRect(-Lr / 2 + body - 0.12 * k, -Wr / 2 + 0.1 * k, 0.08 * k, Wr - 0.2 * k);
        if (r.load) { ctx.fillStyle = '#d6b37a'; ctx.fillRect(-Lr / 2 + body + 0.05 * k, -0.55 * k, 1.1 * k, 1.1 * k); ctx.strokeStyle = '#8a6a2f'; ctx.strokeRect(-Lr / 2 + body + 0.05 * k, -0.55 * k, 1.1 * k, 1.1 * k); }
      } else {
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(-Lr / 2, -Wr / 2, Lr, Wr, Math.min(4, 0.12 * k)); else ctx.rect(-Lr / 2, -Wr / 2, Lr, Wr);
        ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,.85)';
        ctx.fillRect(Lr / 2 - 0.14 * k, -Wr / 2 + 0.08 * k, 0.08 * k, Wr - 0.16 * k);
        if (r.load) { ctx.fillStyle = '#93c5fd'; ctx.fillRect(-0.25 * k, -0.2 * k, 0.4 * k, 0.4 * k); }
      }
      if (r.field === 'PROTECTIVE') { ctx.strokeStyle = '#c2410c'; ctx.lineWidth = 2; ctx.strokeRect(-Lr / 2 - 2, -Wr / 2 - 2, Lr + 4, Wr + 4); }
      ctx.restore();
      if (S.sel && S.sel.kind === 'robot' && S.sel.id === r.id) { ctx.strokeStyle = '#1761c4'; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(px, py, 0.85 * k, 0, Math.PI * 2); ctx.stroke(); }
      if (S.layers.ids && k > 8) {
        ctx.font = '600 9.5px system-ui';
        ctx.fillStyle = r.st === 'PARKED' || r.st === 'IDLE' || r.st === 'CHARGING' ? '#8a9aad' : '#142538';
        ctx.fillText(r.id.replace('AMR-', ''), px + 0.55 * k, py - 0.45 * k);
      }
      if (r.bat < 15) { ctx.fillStyle = '#b42332'; ctx.fillRect(px - 0.45 * k, py + 0.5 * k, 0.9 * k * (r.bat / 100), 2); }
    }
    $('map-meta').textContent = `차량 ${s.robots.length} · 사람 추적 ${s.tracks.filter((x) => x[1] === 'human').length} · 예약 노드 ${Object.keys(s.locks).length} · 버스 손실 ${s.bus.dropped}${s.links_down.length ? ` · 통신 두절 ${s.links_down.join(',')}` : ''}`;
  }

  function entities() {
    const s = S.snap;
    const items = robotPositions().map((r) => ({ kind: 'robot', id: r.id, x: r.dx, y: r.dy, r }));
    for (const t of s.tracks) items.push({ kind: 'track', id: t[0], x: t[2], y: t[3], t });
    for (const x of s.external) items.push({ kind: 'ext', id: x.id, x: x.x, y: x.y, e: x });
    return items;
  }

  canvas.addEventListener('wheel', (e) => {
    if (!S.layout) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    S.view = L.zoomAt(S.view, mapTransform().base, e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, panX: S.view.panX, panY: S.view.panY, moved: false }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', (e) => {
    const rect = canvas.getBoundingClientRect();
    if (drag) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; canvas.classList.add('dragging'); }
      if (drag.moved) { S.view = Object.assign({}, S.view, { panX: drag.panX + dx, panY: drag.panY + dy }); }
      return;
    }
    if (!S.layout || !S.snap) return;
    const w = L.toWorld(mapTransform().t, e.clientX - rect.left, e.clientY - rect.top);
    const hit = L.pick(entities(), w[0], w[1], 1.2);
    const tip = $('tip');
    if (!hit) { tip.hidden = true; return; }
    tip.hidden = false;
    tip.style.left = `${Math.min(e.clientX - rect.left + 12, rect.width - 200)}px`;
    tip.style.top = `${e.clientY - rect.top + 12}px`;
    if (hit.kind === 'robot') tip.textContent = `${hit.id} · ${hit.r.label} · ${hit.r.why || '정상'} · ${Math.round(hit.r.bat)}%`;
    else if (hit.kind === 'track') tip.textContent = `${hit.t[1] === 'human' ? '사람' : '지게차'} ${hit.id} · ${Math.hypot(hit.t[4], hit.t[5]).toFixed(1)} m/s · 관측 ${hit.t[7]}초 전`;
    else tip.textContent = `${hit.id} · 외부 로봇(명령 불가) · 예정 ${hit.e.itinerary.slice(1, 3).join('→')}`;
  });
  canvas.addEventListener('pointerleave', () => { $('tip').hidden = true; });
  canvas.addEventListener('pointerup', (e) => {
    canvas.classList.remove('dragging');
    const wasDrag = drag && drag.moved;
    drag = null;
    if (wasDrag || !S.layout || !S.snap) return;
    const rect = canvas.getBoundingClientRect();
    const w = L.toWorld(mapTransform().t, e.clientX - rect.left, e.clientY - rect.top);
    const hit = L.pick(entities(), w[0], w[1], 1.2);
    S.sel = hit ? { kind: hit.kind, id: hit.id } : null;
    renderAll();
  });
  canvas.addEventListener('dblclick', () => { S.view = { zoom: 1, panX: 0, panY: 0 }; });

  // ------------------------------------------------------------------ tabs
  function renderTabs() {
    const s = S.snap;
    if (!s) return;
    if (S.tab === 'fleet') renderFleet(s);
    else if (S.tab === 'site') renderSite(s);
    else if (S.tab === 'tasks') renderTasks(s);
    else if (S.tab === 'zones') renderZones(s);
    else if (S.tab === 'incidents') renderIncidents(s);
    else if (S.tab === 'log') renderLog(s);
    else if (S.tab === 'jdamr') renderJd(s);
    else if (S.tab === 'drill') renderDrill(s);
  }

  function renderSite(s) {
    const st = s.site_state || {};
    const k = (s.kpi && s.kpi.site) || {};
    if (s.site !== 'dc') { $('site-body').innerHTML = '<div class="empty">격자 창고(알고리즘 검증) 시나리오에는 현장 공정 모델이 없습니다</div>'; return; }
    const lanes = (map, label) => Object.entries(map || {}).map(([n, c]) => `<tr><td>${esc(label)} ${esc(n)}</td><td class="num">${c} / ${st.lane_capacity}</td><td>${'■'.repeat(c)}${'□'.repeat(Math.max(0, st.lane_capacity - c))}</td></tr>`).join('');
    const pickers = s.truth.humans.filter((h) => h[3] === 'picker').length;
    const cells = [
      ['입고 트럭(도착/대기)', `${k.trucks} / ${k.trucks_waiting}`], ['하역 팔레트', k.pallets_unloaded], ['적치 완료', `${k.putaway} (${L.fmtNum(k.putaway_h, 0)}/h)`],
      ['출고 선적', `${k.shipped} (${L.fmtNum(k.shipped_h, 0)}/h)`], ['출고 적체 주문', k.out_backlog], ['랙 점유율', `${Math.round((k.rack_fill || 0) * 100)}%`],
      ['토트 오더 완료', `${k.tote_done} (${L.fmtNum(k.tote_h, 0)}/h)`], ['피킹 건수', k.picks], ['로봇의 피커 대기(누적)', L.fmtDuration(k.picker_wait_s)],
      ['입고→적치 평균', k.dock_to_stock_avg == null ? '—' : L.fmtDuration(k.dock_to_stock_avg)], ['입고→적치 p90', k.dock_to_stock_p90 == null ? '—' : L.fmtDuration(k.dock_to_stock_p90)], ['피커', `${pickers}명 · 존 피킹`],
    ];
    $('site-body').innerHTML = `<div class="jd">${cells.map(([a, b]) => `<div><span class="kicker">${esc(a)}</span><b>${esc(b)}</b></div>`).join('')}</div>
      <div class="grid2"><section><div class="phead"><h2>입고 스테이징 레인</h2><span class="meta">수동 지게차 하역 → 자율 지게차 적치</span></div><table><tbody>${lanes(st.in_stage, '입고')}</tbody></table></section>
      <section><div class="phead"><h2>출고 스테이징 레인</h2><span class="meta">자율 지게차 출고 → 수동 지게차 상차</span></div><table><tbody>${lanes(st.out_stage, '출고')}</tbody></table></section></div>`;
  }

  function renderFleet(s) {
    const rows = L.exceptionSort(s.robots);
    $('fleet-rows').innerHTML = rows.map((r) => {
      const info = L.statusInfo(r.st);
      const cls = { FORKLIFT: '자율 지게차', CARRIER: '토트 AMR' }[r.cls] || 'AMR';
      return `<tr data-robot="${esc(r.id)}" class="${S.sel && S.sel.id === r.id ? 'sel' : ''}"><td><b>${esc(r.id)}</b><br><span class="meta" style="text-align:left">${esc(cls)}</span></td><td><span class="tag ${info.tone}">${esc(r.label)}</span></td><td>${esc(r.why)}</td><td class="num">${L.fmtNum(r.bat, 0)}%${r.chg ? ' ⚡' : ''}</td><td>${esc(r.task || '—')}</td><td>${esc(r.goal || '—')}</td><td class="num">${r.wait_s ? L.fmtDuration(r.wait_s) : '—'}</td><td>${(() => { const li = L.linkInfo(r.conn, r.link, r.lat_ms); return li.tone === 'ok' ? esc(li.text) : `<span class="tag ${li.tone}">${esc(li.text)}</span>`; })()}</td><td class="num">${L.fmtNum(r.v, 2)}</td></tr>`;
    }).join('');
  }
  $('fleet-rows').addEventListener('click', (e) => { const tr = e.target.closest('[data-robot]'); if (tr) { S.sel = { kind: 'robot', id: tr.dataset.robot }; renderAll(); } });

  function renderTasks(s) {
    if (document.activeElement !== $('tf-rate')) $('tf-rate').value = s.task_rate;
    if (!$('tf-kind').dataset.k) fillTaskSelects();
    $('task-rows').innerHTML = s.tasks.map((t) => `<tr><td><b>${esc(t.id)}</b></td><td><span class="tag ${t.state === 'QUEUED' ? (t.age > 600 ? 'warn' : '') : 'normal'}">${esc(t.label)}${t.state === 'ACTIVE' ? ` ${t.leg + 1}/${t.legs}` : ''}</span></td><td>${esc(t.kind)}</td><td>${esc((t.stops || [t.src, t.dst || '(배정 시 선택)']).join(' → '))}</td><td class="num">${t.pri}</td><td>${esc(t.robot || '—')}</td><td class="num">${L.fmtDuration(t.age)}</td><td class="num">${t.attempts}</td><td>${esc(t.note)}</td><td style="white-space:nowrap"><button class="btn sm" type="button" data-task="${esc(t.id)}" data-a="pri">우선↑</button> ${t.robot ? `<button class="btn sm" type="button" data-task="${esc(t.id)}" data-a="reassign">재할당</button>` : ''} <button class="btn sm" type="button" data-task="${esc(t.id)}" data-a="cancel">취소</button></td></tr>`).join('') || '<tr><td colspan="10" class="empty">작업 없음</td></tr>';
  }
  $('task-rows').addEventListener('click', (e) => {
    const b = e.target.closest('[data-task]');
    if (!b) return;
    const t = S.snap.tasks.find((x) => x.id === b.dataset.task);
    if (b.dataset.a === 'pri') command('reprioritize_task', { task: b.dataset.task, priority: Math.min(2, (t ? t.pri : 0) + 1) });
    if (b.dataset.a === 'reassign') command('reassign_task', { task: b.dataset.task });
    if (b.dataset.a === 'cancel') command('cancel_task', { task: b.dataset.task });
  });
  $('tf-kind').onchange = fillTaskSelects;
  $('tf-add').onclick = () => command('add_task', { kind: $('tf-kind').value, source: $('tf-src').value, dest: $('tf-dst').value || null, priority: Number($('tf-pri').value) });
  $('tf-rate-set').onclick = () => command('set_task_rate', { rate: Number($('tf-rate').value) });

  function fillTaskSelects() {
    if (!S.layout || !S.snap) return;
    const kinds = S.snap.task_kinds || [];
    const cur = $('tf-kind').value;
    const kindKey = kinds.map((x) => x.kind).join(',');
    if ($('tf-kind').dataset.k !== kindKey) {
      $('tf-kind').innerHTML = kinds.map((x) => `<option value="${esc(x.kind)}">${esc(x.label)}</option>`).join('');
      $('tf-kind').dataset.k = kindKey;
    }
    const spec = kinds.find((x) => x.kind === ($('tf-kind').value || cur)) || kinds[0];
    if (!spec) return;
    const ids = (ks) => S.layout.nodes.filter((n) => ks.includes(n.kind)).map((n) => n.id).sort();
    const opts = (list, empty) => (empty ? '<option value="">자동 선택</option>' : '') + list.map((n) => `<option>${esc(n)}</option>`).join('');
    $('tf-src').innerHTML = opts(ids(spec.src));
    $('tf-dst').innerHTML = opts(ids(spec.dst), spec.dst_optional);
  }

  function fillStaticSelects() {
    fillTaskSelects();
    const zones = S.layout.zones.map((z) => `<option value="${esc(z.id)}">${esc(z.name)}</option>`).join('');
    $('dr-zone').innerHTML = zones;
  }

  function renderZones(s) {
    if (!S.layout) return;
    const dyn = Object.fromEntries(s.zones.map((z) => [z.id, z]));
    const kindLabel = { human_work: '사람 작업', walkway: '보행', picking: '수동 피킹', forklift_lane: '지게차', charging: '충전', aisle: '랙 통로' };
    renderDoors(s);
    $('zone-rows').innerHTML = S.layout.zones.map((z) => {
      const d = dyn[z.id] || {};
      return `<tr><td><b>${esc(z.name)}</b><br><span class="meta" style="text-align:left">${esc(z.id)}</span></td><td>${esc(kindLabel[z.kind] || z.kind)}</td><td>${d.limit != null ? `<span class="tag warn">${d.limit} m/s</span>` : '—'}${d.override != null ? ` · 수동 ${d.override}` : ''}${z.static_speed ? ` · 기본 ${z.static_speed}` : ''}</td><td>${d.locked ? '<span class="tag danger">봉쇄</span>' : '—'}</td><td style="white-space:nowrap">${d.locked ? `<button class="btn sm" type="button" data-z="${esc(z.id)}" data-a="unlock">해제</button>` : `<button class="btn sm warn" type="button" data-z="${esc(z.id)}" data-a="lock">봉쇄</button>`} <button class="btn sm danger" type="button" data-z="${esc(z.id)}" data-a="evac">대피</button> <select class="ctl" data-zs="${esc(z.id)}" style="height:26px"><option value="">속도 제한</option><option value="0.3">0.3</option><option value="0.5">0.5</option><option value="0.8">0.8</option><option value="off">해제</option></select></td></tr>`;
    }).join('');
  }
  function renderDoors(s) {
    const doors = s.doors || [];
    $('door-wrap').hidden = !doors.length;
    const mode = { OPEN: '열림', CLOSED: '닫힘', MOVING: '동작 중', UNKNOWN: '미수신' };
    const cause = { stale: '제어기 무응답', fault: '고장 신호', timeout: '열림 요청 후 미개방' };
    $('door-rows').innerHTML = doors.map((d) => {
      const state = d.disabled ? '<span class="tag danger">사용 중지</span>' : d.cause ? `<span class="tag danger">${esc(cause[d.cause] || d.cause)}</span>` : esc(mode[d.mode] || d.mode);
      return `<tr><td><b>${esc(d.name)}</b><br><span class="meta" style="text-align:left">${esc(d.id)}</span></td><td>${state}</td><td>${d.requesters.length ? esc(d.requesters.join(', ')) : '—'}${d.waiting_s ? ` · ${L.fmtDuration(d.waiting_s)}` : ''}</td><td class="num">${d.age == null ? '—' : `${d.age}초 전`}</td><td class="num">${d.opens}</td><td style="white-space:nowrap">${d.disabled || d.cause ? `<button class="btn sm" type="button" data-door="${esc(d.id)}" data-a="door_retry">재시도</button>` : `<button class="btn sm warn" type="button" data-door="${esc(d.id)}" data-a="door_disable">사용 중지</button>`} <button class="btn sm" type="button" data-door="${esc(d.id)}" data-a="door_field_fix">[현장 모의] 수리</button></td></tr>`;
    }).join('');
  }
  $('door-rows').addEventListener('click', (e) => {
    const b = e.target.closest('[data-door]');
    if (b) command(b.dataset.a, { door: b.dataset.door });
  });
  $('zone-rows').addEventListener('click', (e) => {
    const b = e.target.closest('[data-z]');
    if (!b) return;
    const zone = b.dataset.z;
    if (b.dataset.a === 'lock') run('zone_lock', { zone });
    if (b.dataset.a === 'unlock') run('zone_unlock', { zone });
    if (b.dataset.a === 'evac') run('zone_evacuate', { zone });
  });
  $('zone-rows').addEventListener('change', (e) => {
    const sel = e.target.closest('[data-zs]');
    if (!sel || !sel.value) return;
    command('zone_speed', { zone: sel.dataset.zs, limit: sel.value === 'off' ? null : Number(sel.value) });
  });

  // incidents & replay
  function renderIncidents(s) {
    $('incident-rows').innerHTML = s.incidents.map((i) => {
      const ack = i.acked_at != null ? L.fmtDuration(i.acked_at - i.opened_at) : '—';
      const clr = i.cleared_at != null ? L.fmtDuration(i.cleared_at - i.opened_at) : '—';
      const state = i.closed_at != null ? '종결' : i.recording ? '녹화 중' : '조사 대기';
      return `<tr data-inc="${esc(i.id)}" class="${S.replay.data && S.replay.data.id === i.id ? 'sel' : ''}"><td><b>${esc(i.id)}</b></td><td><span class="prio p${i.priority}">${i.priority === 1 ? '긴급' : '보통'}</span> ${esc(i.kind)}</td><td>${esc(i.source)}</td><td class="num">${L.fmtClock(i.opened_at)}</td><td class="num">${ack}</td><td class="num">${clr}</td><td>${state}</td></tr>`;
    }).join('') || '<tr><td colspan="7" class="empty">기록된 사고 없음</td></tr>';
  }
  $('incident-rows').addEventListener('click', async (e) => {
    const tr = e.target.closest('[data-inc]');
    if (!tr) return;
    const r = await api(`/api/incident/${encodeURIComponent(tr.dataset.inc)}`);
    if (!r.ok) return;
    S.replay.data = r.body;
    S.replay.idx = r.body.frame_data.findIndex((f) => f.t >= r.body.opened_at);
    if (S.replay.idx < 0) S.replay.idx = r.body.frame_data.length - 1;
    $('rp-slider').max = Math.max(0, r.body.frame_data.length - 1);
    $('rp-slider').value = S.replay.idx;
    renderIncidentInfo();
    drawReplay();
  });
  function renderIncidentInfo() {
    const d = S.replay.data;
    if (!d) return;
    const truth = d.truth && d.truth.kind ? `<div class="note" style="border:0;padding:6px 0">평가기(실좌표): ${esc(d.truth.robot)} ↔ ${esc(d.truth.other)} (${esc(d.truth.kind)}) · 속도 ${d.truth.speed} m/s · ${d.truth.moving ? '주행 중' : '정지 중'} 접촉</div>` : '';
    $('incident-info').innerHTML = `<div class="unit-head" style="padding:0 0 6px"><strong>${esc(d.id)}</strong><span class="prio p${d.priority}">${d.priority === 1 ? '긴급' : '보통'}</span></div>
      <div><b>${esc(d.kind)}</b> · ${esc(d.source)}</div><div class="m" style="color:var(--muted)">${esc(d.message)}</div>${truth}
      <div class="kv" style="margin:8px -11px"><div><span class="kicker">발생</span><b>${L.fmtClock(d.opened_at)}</b></div><div><span class="kicker">확인까지</span><b>${d.acked_at != null ? L.fmtDuration(d.acked_at - d.opened_at) : '—'}</b></div><div><span class="kicker">해제까지</span><b>${d.cleared_at != null ? L.fmtDuration(d.cleared_at - d.opened_at) : '—'}</b></div></div>
      ${d.closed_at != null ? `<div><span class="kicker">종결 기록</span>${esc(d.note)}</div>` : `<label class="kicker" for="inc-note">종결 기록(원인·조치·재발 방지)</label><textarea id="inc-note" rows="3" style="width:100%;border:1px solid var(--line);border-radius:4px;padding:6px"></textarea><div class="row"><button class="btn primary sm" type="button" id="inc-close">사고 종결</button></div>`}`;
    const btn = $('inc-close');
    if (btn) btn.onclick = () => command('close_incident', { id: d.id, note: $('inc-note').value });
  }
  $('rp-slider').oninput = () => { S.replay.idx = Number($('rp-slider').value); drawReplay(); };
  $('rp-play').onclick = () => {
    if (!S.replay.data) return;
    S.replay.playing = !S.replay.playing;
    $('rp-play').textContent = S.replay.playing ? '멈춤' : '재생';
    clearInterval(S.replay.timer);
    if (S.replay.playing) {
      S.replay.timer = setInterval(() => {
        const n = S.replay.data.frame_data.length;
        S.replay.idx = (S.replay.idx + 1) % n;
        $('rp-slider').value = S.replay.idx;
        drawReplay();
      }, 250);
    }
  };
  function drawReplay() {
    const d = S.replay.data;
    const c = $('replay');
    if (!d || !S.layout || !d.frame_data.length) return;
    const ctx = setupCanvas(c);
    const f = d.frame_data[Math.min(S.replay.idx, d.frame_data.length - 1)];
    const t = L.fitTransform(S.layout.width, S.layout.height, c.clientWidth, c.clientHeight, 10);
    const P = (x, y) => L.toScreen(t, x, y);
    const k = t.scale;
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.clientWidth, c.clientHeight);
    ctx.fillStyle = '#e2e8f0';
    for (const r of S.layout.racks) { ctx.beginPath(); r.forEach(([x, y], i) => { const [px, py] = P(x, y); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }); ctx.fill(); }
    const focus = d.source.split(/[/,]/)[0];
    for (const r of f.r) {
      const [px, py] = P(r[1], r[2]);
      ctx.save(); ctx.translate(px, py); ctx.rotate(-r[3]);
      ctx.fillStyle = r[0] === focus ? '#1761c4' : L.statusInfo(r[4]).color;
      ctx.fillRect(-0.45 * k, -0.35 * k, 0.9 * k, 0.7 * k);
      ctx.restore();
      if (r[0] === focus) { ctx.strokeStyle = '#1761c4'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(px, py, 1.2 * k, 0, Math.PI * 2); ctx.stroke(); }
    }
    for (const h of f.h) { const [px, py] = P(h[1], h[2]); ctx.fillStyle = '#0f172a'; ctx.beginPath(); ctx.arc(px, py, Math.max(3, 0.3 * k), 0, Math.PI * 2); ctx.fill(); }
    for (const fl of f.f) { const [px, py] = P(fl[1], fl[2]); ctx.save(); ctx.translate(px, py); ctx.rotate(-fl[3]); ctx.fillStyle = '#a16207'; ctx.fillRect(-1.2 * k, -0.6 * k, 2.4 * k, 1.2 * k); ctx.restore(); }
    for (const e of f.e) { const [px, py] = P(e[1], e[2]); ctx.fillStyle = '#0e7490'; ctx.fillRect(px - 4, py - 4, 8, 8); }
    for (const tr of f.k) { const [px, py] = P(tr[2], tr[3]); ctx.strokeStyle = 'rgba(194,65,12,.7)'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(px, py, 0.6 * k, 0, Math.PI * 2); ctx.stroke(); }
    const rel = f.t - d.opened_at;
    $('rp-time').textContent = `${rel >= 0 ? '+' : ''}${rel.toFixed(1)}초 (${L.fmtClock(f.t)}) · 파랑=대상 로봇, 주황 원=관제 추적`;
  }

  // logs
  function renderLog(s) {
    const lvl = $('ev-filter').value;
    const icon = { crit: 'danger', warn: 'warn', info: '' };
    $('event-rows').innerHTML = s.events.slice().reverse().filter((e) => !lvl || e.level === lvl).map((e) => `<tr><td class="num">${L.fmtClock(e.t)}</td><td><span class="tag ${icon[e.level] || ''}">${esc(e.type)}</span></td><td>${esc(e.source)}</td><td>${esc(e.msg)}</td></tr>`).join('');
    $('audit-rows').innerHTML = s.audit.slice().reverse().map((a) => `<tr><td class="num">${L.fmtClock(a.t)}</td><td>${esc(a.operator)}</td><td>${esc(a.cmd)}</td><td>${esc(Object.values(a.args || {}).filter((v) => typeof v !== 'object').join(' · '))}</td><td>${a.ok ? '<span class="tag ok">수락</span>' : '<span class="tag danger">거부</span>'} ${esc(a.msg)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">기록 없음</td></tr>';
  }
  $('ev-filter').onchange = () => S.snap && renderLog(S.snap);

  // shift handover: the note is audited; the summary is generated from live state
  $('ho-save').onclick = async () => {
    if (await command('handover', { note: $('ho-note').value, by: operator() })) $('ho-note').value = '';
  };
  $('ho-dl').onclick = async () => {
    const r = await api('/api/handover');
    if (!r.ok) return;
    const h = r.body.summary;
    const list = (title, items) => `\n## ${title} (${items.length})\n` + (items.length ? items.map((x) => `- ${x}`).join('\n') : '- 없음');
    const text = `# 교대 인수인계 · ${h.scenario} · ${L.fmtClock(h.sim_t)} · 작성 ${operator()}\n운영 모드: ${h.mode}\n` +
      list('활성 알람', h.alarms) + list('미종결 사고', h.incidents_open) + list('고장 로봇', h.faulted) +
      list('통신 두절', h.offline) + list('원격 조종 중', h.manual) + list('일시정지 로봇', h.paused) +
      list('봉쇄 구역', h.zones_locked) + list('교착', h.deadlocks) +
      `\n## 작업\n- 대기 ${h.tasks_queued} · 진행 ${h.tasks_active}\n## 지표\n- 처리량/h ${h.kpi.throughput_h} · 교통 대기 ${Math.round(h.kpi.traffic_wait_ratio * 100)}% · 주행 중 접촉 ${h.kpi.contacts} · 근접사고 ${h.kpi.near_miss} · 알람/h ${h.kpi.alarms_per_h}\n` +
      (r.body.history.length ? `\n## 이전 인수인계 메모\n${r.body.history.map((x) => `- ${L.fmtClock(x.t)} ${x.by}: ${x.note}`).join('\n')}\n` : '');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `handover-${L.fmtClock(h.sim_t).replace(/:/g, '')}.txt`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  // VDA 5050 inspector
  async function loadVda() {
    if (!S.snap) return;
    const sel = $('vda-robot');
    if (!sel.options.length) sel.innerHTML = S.snap.robots.map((r) => `<option>${esc(r.id)}</option>`).join('');
    if (S.sel && S.sel.kind === 'robot' && sel.value !== S.sel.id && !S.vda.pinned) sel.value = S.sel.id;
    const r = await api(`/api/vda5050/${encodeURIComponent(sel.value)}?limit=20`);
    if (!r.ok) return;
    S.vda.msgs = r.body.slice().reverse();
    $('vda-list').innerHTML = S.vda.msgs.map((m, i) => `<button type="button" data-i="${i}" class="${i === S.vda.sel ? 'sel' : ''}"><span>${m.t.toFixed(1)}s</span><span><b>${esc(m.topic.split('/').pop())}</b> ${esc(m.topic)}</span><span>${m.dropped ? '<span class="tag danger">손실</span>' : `${m.bytes} B`}</span></button>`).join('') || '<div class="empty">메시지 없음</div>';
    showVda();
  }
  function showVda() {
    const m = S.vda.msgs[S.vda.sel];
    $('vda-json').textContent = m ? JSON.stringify(m.payload, null, 2) : '메시지를 선택하세요';
    $('vda-meta').textContent = m ? `${m.topic} · headerId ${m.payload.headerId} · ${m.dropped ? '링크에서 손실' : '전달'}` : '—';
  }
  $('vda-list').addEventListener('click', (e) => { const b = e.target.closest('[data-i]'); if (b) { S.vda.sel = Number(b.dataset.i); $('vda-list').querySelectorAll('button').forEach((x) => x.classList.toggle('sel', x === b)); showVda(); } });
  $('vda-robot').onchange = () => { S.vda.pinned = true; S.vda.sel = 0; loadVda(); };
  setInterval(() => { if (S.tab === 'vda') loadVda(); }, 1500);

  // JD-AMR
  function renderJd(s) {
    const jd = s.jdamr;
    if (!jd || jd.enabled === false) { $('jd-body').innerHTML = '<div class="empty">서버가 --no-jdamr로 실행되었습니다</div>'; return; }
    const d = jd.data || {};
    const cells = [
      ['연결', jd.connected ? `정상 · ${jd.age_s}초 전` : `끊김 · ${jd.error || ''}`], ['운전 상태', d.drive || '미수신'],
      ['차단·감속 근거', d.blockers && d.blockers.length ? d.blockers.join(' · ') : '없음'], ['배터리', d.battery_pct != null ? `${Math.round(d.battery_pct)}% · ${d.battery_v} V` : '미수신'],
      ['속도', d.speed_mps != null ? `${Number(d.speed_mps).toFixed(2)} m/s` : '미수신'], ['위치(자체 지도)', d.pose ? `${Number(d.pose.x).toFixed(2)}, ${Number(d.pose.y).toFixed(2)} · ${Number(d.pose.yaw_deg).toFixed(0)}°` : '미수신'],
      ['관제 가동', d.uptime_s != null ? L.fmtDuration(d.uptime_s) : '—'], ['조회 주소', jd.url],
    ];
    $('jd-body').innerHTML = `<div class="jd">${cells.map(([k2, v]) => `<div><span class="kicker">${esc(k2)}</span><b>${esc(v)}</b></div>`).join('')}</div>`;
  }

  // drills
  function renderDrill(s) {
    const opts = s.robots.map((r) => `<option>${esc(r.id)}</option>`).join('');
    for (const id of ['dr-robot', 'dr-robot2']) { const el = $(id); if (el.options.length !== s.robots.length) el.innerHTML = opts; }
    const doors = s.doors || [];
    $('dr-door-form').hidden = !doors.length;
    if ($('dr-door').options.length !== doors.length) $('dr-door').innerHTML = doors.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
    $('auto-op').checked = s.run.auto_operator;
  }
  $('dr-fault-go').onclick = () => command('drill_fault', { robot: $('dr-robot').value, fault: $('dr-fault').value, recoverable: $('dr-rec').value === '1' });
  $('dr-comms-go').onclick = () => command('drill_comms', { robot: $('dr-robot2').value, seconds: Number($('dr-sec').value) });
  $('dr-intr-go').onclick = () => command('drill_intruder', { zone: $('dr-zone').value, seconds: Number($('dr-isec').value) });
  $('dr-door-go').onclick = () => command('drill_door', { door: $('dr-door').value, fault: $('dr-dfault').value });
  $('rs-go').onclick = () => ask('시나리오 재시작', '같은 시나리오를 입력한 시드로 다시 시작합니다.', false, () => control('reset', null, { scenario: $('scenario').value, strategy: $('strategy').value, seed: Number($('rs-seed').value) || 7 }));
  $('auto-op').onchange = () => control('auto_operator', $('auto-op').checked);

  // trends
  async function loadHistory() {
    const r = await api('/api/history');
    if (!r.ok) return;
    S.history = r.body;
    const h = S.history;
    const series = [['처리량/시간', h.map((x) => x.thr), (v) => L.fmtNum(v, 0), '#1761c4'], ['교통 대기 비율', h.map((x) => x.wait), (v) => `${Math.round((v || 0) * 100)}%`, '#a16207'], ['활성 알람', h.map((x) => x.alarms), (v) => L.fmtNum(v, 0), '#b42332']];
    $('charts').innerHTML = series.map(([label, vals, fmt, col]) => `<div class="chart"><span class="kicker">${esc(label)}</span><b>${esc(fmt(vals[vals.length - 1]))}</b><svg viewBox="0 0 300 90" preserveAspectRatio="none" aria-label="${esc(label)} 추세"><path d="${L.sparkPath(vals, 300, 86)}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg><span class="meta" style="text-align:left;display:block">표본 ${vals.length}개 · ${h.length ? L.fmtClock(h[0].t) : '—'} ~ ${h.length ? L.fmtClock(h[h.length - 1].t) : '—'}</span></div>`).join('');
  }
  setInterval(() => { if (S.tab === 'trend') loadHistory(); }, 5000);

  // ------------------------------------------------------------------ start
  let started = false;
  function start() {
    if (started) { connect(); return; }
    started = true;
    initMeta().then(connect);
  }
  selectTab(S.tab);
  ensureSession().then((ok) => { if (ok) start(); });
  requestAnimationFrame(drawMap);
})();
