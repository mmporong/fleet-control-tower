"""Control room web server: runs the simulation in real time and serves the UI.

Standard library only. Binds to loopback by default. Every API call needs a
signed-in account (``fleet.auth``): viewers read, operators command, supervisors
also control the simulation. Upper systems use an API key for the WMS endpoints.
Commands go through ``Operations`` (validated, audited with the account name,
hash-chained in the SQLite store); ``--read-only`` turns the whole server into
an observation console.

    python3 -m fleet.server --scenario standard --port 8095
    python3 -m fleet.server --add-user kim:operator      # password from the prompt or FCT_NEW_PASSWORD
    python3 -m fleet.server --create-api-key wms-1       # prints the key once
"""
from __future__ import annotations

import argparse
from collections import deque
import getpass
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .auth import Auth, AuthError, Session, at_least, check_state_change, cookie_token, session_cookie
from .autoop import AutoOperator
from .control import STRATEGIES
from .jdamr import DEFAULT_URL, JdamrReadOnly
from .ops import Operations
from .scenario import PRESETS, get
from .sim import Simulation
from .store import Store

WEB = Path(__file__).resolve().parents[1] / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/logic.js": ("logic.js", "text/javascript; charset=utf-8")}
MAX_BODY = 64 * 1024
SPEEDS = (0.5, 1.0, 2.0, 5.0, 10.0)
DEFAULT_DB = Path.home() / ".local" / "state" / "fleet-control-tower" / "server.sqlite3"
# commands that only a supervisor may give: training drills inject faults into the simulation
SUPERVISOR_COMMANDS = {"drill_fault", "drill_comms", "drill_intruder", "drill_door", "door_field_fix", "set_task_rate"}


class IdemTable:
    """WMS idempotency keys in the store, with the dict interface ``Operations.submit_wms_order`` uses."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def __contains__(self, oid: str) -> bool:
        return self.store.idem_get(oid) is not None

    def __getitem__(self, oid: str) -> dict:
        row = self.store.idem_get(oid)
        if row is None:
            raise KeyError(oid)
        return row

    def __setitem__(self, oid: str, value: dict) -> None:
        self.store.idem_put(oid, value["body"], value["task"])


class Runner(threading.Thread):
    """Advances the simulation against wall time and keeps a snapshot cache."""

    def __init__(self, scenario: str, seed: int, strategy: str, speed: float, auto_operator: bool,
                 read_only: bool, store: Store | None = None) -> None:
        super().__init__(daemon=True, name="sim-runner")
        self.lock = threading.RLock()
        self.store = store or Store()
        self.read_only = read_only
        self.auto_operator = auto_operator
        self.speed = speed
        self.paused = False
        self.lag_s = 0.0
        self._stop = threading.Event()
        self._snap: dict | None = None
        self._snap_at = 0.0
        self.reset(scenario, seed, strategy)

    def reset(self, scenario: str, seed: int, strategy: str) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
        with self.lock:
            self.sim = Simulation(get(scenario), seed=seed, strategy=strategy)
            self.ops = Operations(self.sim, read_only=self.read_only, store=self.store)
            self.autoop = AutoOperator(self.ops, random.Random(seed + 99))
            if self.auto_operator:
                self.sim.tick_hooks.append(self.autoop.tick)
            self.history: deque = deque(maxlen=720)
            self._next_sample = 0.0
            self.wms_seen = IdemTable(self.store)
            self._snap = None

    def set_auto_operator(self, on: bool) -> None:
        with self.lock:
            self.auto_operator = on
            hooks = self.sim.tick_hooks
            if on and self.autoop.tick not in hooks:
                hooks.append(self.autoop.tick)
            if not on and self.autoop.tick in hooks:
                hooks.remove(self.autoop.tick)

    def effective_speed(self) -> float:
        manual = any(r.mode == "MANUAL" for r in self.sim.manager.robots.values())
        return min(self.speed, 1.0) if manual else self.speed   # teleop runs in real time

    def run(self) -> None:
        last = time.monotonic()
        budget = 0.0
        while not self._stop.is_set():
            time.sleep(0.02)
            now = time.monotonic()
            real_dt = now - last
            last = now
            if self.paused:
                budget = 0.0
                continue
            with self.lock:
                budget += real_dt * self.effective_speed()
                steps = int(budget / self.sim.dt)
                if steps > 200:                 # cannot keep up: drop time instead of spiralling
                    self.lag_s += (steps - 200) * self.sim.dt
                    steps = 200
                    budget = steps * self.sim.dt
                for _ in range(steps):
                    self.sim.step()
                    if self.sim.now >= self._next_sample:
                        self._next_sample = self.sim.now + 10.0
                        self._sample()
                budget -= steps * self.sim.dt

    def _sample(self) -> None:
        k = self.sim.kpi()
        c = self.sim.manager.alarms.counts()
        self.history.append({"t": round(self.sim.now), "done": k["done"], "thr": k["throughput_h"],
                             "wait": k["traffic_wait_ratio"], "queued": k["queued"], "working": k["working"],
                             "alarms": c["p1"] + c["p2"] + c["p3"], "pstop": k["protective_stops"],
                             "nm": k["near_miss_truth"]})

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            if self._snap is None or now - self._snap_at > 0.15:
                snap = self.sim.snapshot()
                snap["run"] = {"speed": self.speed, "effective_speed": self.effective_speed(),
                               "paused": self.paused, "lag_s": round(self.lag_s, 1),
                               "auto_operator": self.auto_operator, "read_only": self.read_only,
                               "interventions": self.ops.counts["interventions"]}
                snap["audit"] = list(self.ops.audit)[-40:]
                self._snap = snap
                self._snap_at = now
            return self._snap

    def local_view(self, serial: str) -> dict:
        """What the operator can see around one robot (for remote driving): FMS data only."""
        with self.lock:
            m = self.sim.manager
            rec = m.robots[serial]
            cx, cy = rec.x, rec.y
            near = lambda x, y: math.hypot(x - cx, y - cy) < 7.0   # noqa: E731
            return {
                "robot": m.robot_view(rec),
                "robots": [[r.serial, round(r.x, 2), round(r.y, 2), round(r.theta, 2)]
                           for r in m.robots.values() if r.serial != serial and near(r.x, r.y)],
                "tracks": [[t.id, t.cls, round(t.x, 2), round(t.y, 2)] for t in m.tracker.tracks.values()
                           if near(t.x, t.y)],
                "detections": rec.detections,
                "external": [[k, round(v["x"], 2), round(v["y"], 2)] for k, v in m.external.items()
                             if near(v["x"], v["y"])],
                "racks": [r for r in self.sim.layout.racks
                          if any(near(px, py) for px, py in r) or
                          (r[0][0] - 7 < cx < r[2][0] + 7 and r[0][1] - 7 < cy < r[2][1] + 7)],
                "field": {"state": rec.safety_field, "cause": rec.field_cause},
            }

    def stop(self) -> None:
        self._stop.set()


def make_handler(runner: Runner, jdamr: JdamrReadOnly | None, auth: Auth | None, secure_cookie: bool = False):
    """``auth`` None turns authentication off (``--no-auth``, loopback development only)."""
    open_session = Session("local", "supervisor", "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "FleetControlTower/0.2"

        def log_message(self, fmt: str, *args) -> None:   # keep the console quiet
            return

        # ---------------------------------------------------------- helpers
        def _json(self, obj, status: int = 200, headers: dict | None = None) -> None:
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("요청 본문이 너무 큽니다")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다")
            return data

        def _session(self) -> Session | None:
            if auth is None:
                return open_session
            return auth.session(cookie_token(self.headers.get("Cookie")))

        def _api_key(self) -> Session | None:
            header = self.headers.get("Authorization") or ""
            if auth is None:
                return open_session
            return auth.api_key(header[7:].strip()) if header.startswith("Bearer ") else None

        def _require(self, role: str, state_change: bool = False) -> Session | None:
            """The signed-in session with at least ``role``; answers 401/403 and returns None otherwise."""
            sess = self._session()
            if sess is None:
                self._json({"ok": False, "msg": "로그인이 필요합니다", "login": True}, 401)
                return None
            if not at_least(sess.role, role):
                self._json({"ok": False, "msg": f"권한 부족: {role} 이상 필요 (현재 {sess.role})"}, 403)
                return None
            if state_change and auth is not None:
                try:
                    check_state_change(self.headers, sess, self.headers.get("Host"))
                except AuthError as exc:
                    self._json({"ok": False, "msg": str(exc)}, exc.status)
                    return None
            return sess

        # ---------------------------------------------------------- GET
        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path in STATIC:
                name, ctype = STATIC[path]
                data = (WEB / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/health":
                self._json({"ok": True, "sim_t": runner.sim.now})
                return
            if path == "/api/me":
                sess = self._session()
                if sess is None:
                    self._json({"ok": False, "login": True}, 401)
                else:
                    self._json({"ok": True, "name": sess.name, "role": sess.role, "csrf": sess.csrf,
                                "auth": auth is not None})
                return
            if path == "/api/wms/events":
                if self._api_key() is None and self._require("operator") is None:
                    return
                after = int((parse_qs(url.query).get("after") or ["0"])[0])
                with runner.lock:
                    self._json(runner.sim.dispatcher.events_after(after))
                return
            if self._require("viewer") is None:
                return
            if path == "/api/layout":
                with runner.lock:
                    self._json(runner.sim.layout.to_json())
            elif path == "/api/meta":
                self._json({"scenarios": {k: v.to_json() for k, v in PRESETS.items()},
                            "strategies": list(STRATEGIES), "speeds": list(SPEEDS)})
            elif path == "/api/snapshot":
                self._json(runner.snapshot())
            elif path == "/api/stream":
                self._stream()
            elif path == "/api/history":
                with runner.lock:
                    self._json(list(runner.history))
            elif path == "/api/audit":
                with runner.lock:
                    self._json(list(runner.ops.audit))
            elif path == "/api/audit/verify":
                self._json({"problems": runner.store.verify_audit()})
            elif path.startswith("/api/incident/"):
                try:
                    with runner.lock:
                        self._json(runner.sim.incident(path.rsplit("/", 1)[1]))
                except KeyError:
                    self._json({"error": "unknown incident"}, 404)
            elif path.startswith("/api/vda5050/"):
                serial = path.rsplit("/", 1)[1]
                limit = int((parse_qs(url.query).get("limit") or ["12"])[0])
                with runner.lock:
                    if serial not in runner.sim.manager.robots:
                        self._json({"error": "unknown robot"}, 404)
                        return
                    self._json(runner.sim.bus.recent(serial, max(1, min(30, limit))))
            elif path.startswith("/api/local/"):
                try:
                    self._json(runner.local_view(path.rsplit("/", 1)[1]))
                except KeyError:
                    self._json({"error": "unknown robot"}, 404)
            elif path == "/api/handover":
                with runner.lock:
                    self._json({"summary": runner.ops.handover_summary(), "history": runner.sim.handovers[-20:]})
            elif path == "/api/jdamr":
                self._json(jdamr.status() if jdamr else {"enabled": False})
            else:
                self._json({"error": "not found"}, 404)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            token = cookie_token(self.headers.get("Cookie"))
            try:
                while True:
                    if auth is not None and auth.session(token) is None:
                        self.wfile.write(b"event: logout\ndata: {}\n\n")      # session ended: stop streaming
                        self.wfile.flush()
                        return
                    snap = runner.snapshot()
                    if jdamr:
                        snap = {**snap, "jdamr": jdamr.status()}
                    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"event: snapshot\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        # ---------------------------------------------------------- POST
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"ok": False, "msg": f"잘못된 요청: {exc}"}, 400)
                return
            if path == "/api/login":
                self._login(body)
            elif path == "/api/logout":
                if auth is not None:
                    auth.logout(cookie_token(self.headers.get("Cookie")))
                self._json({"ok": True}, headers={"Set-Cookie": session_cookie("", secure_cookie, clear=True)})
            elif path == "/api/command":
                name = str(body.get("cmd", ""))
                sess = self._require("supervisor" if name in SUPERVISOR_COMMANDS else "operator", state_change=True)
                if sess is None:
                    return
                with runner.lock:
                    res = runner.ops.execute(body, sess.name)
                    runner._snap = None
                self._json(res, 403 if runner.read_only else 200 if res["ok"] else 400)
            elif path == "/api/wms/orders":
                key = self._api_key()
                sess = key if key is not None and key.role == "wms" else \
                    self._require("supervisor", state_change=True)
                if sess is None:
                    return
                if runner.read_only:
                    self._json({"ok": False, "msg": "관찰 전용 모드"}, 403)
                    return
                try:
                    with runner.lock:
                        res = runner.ops.submit_wms_order(body, runner.wms_seen, operator=f"WMS:{sess.name}")
                    self._json(res, 200 if res.get("duplicate") else 201)
                except ValueError as exc:
                    self._json({"ok": False, "msg": str(exc)}, 400 if "이미" not in str(exc) else 409)
            elif path == "/api/control":
                sess = self._require("supervisor", state_change=True)
                if sess is None:
                    return
                if runner.read_only:
                    self._json({"ok": False, "msg": "관찰 전용 모드"}, 403)
                    return
                self._control(body, sess)
            else:
                self._json({"error": "not found"}, 404)

        def _login(self, body: dict) -> None:
            if auth is None:
                self._json({"ok": True, "name": "local", "role": "supervisor", "csrf": "", "auth": False})
                return
            try:
                token, sess = auth.login(str(body.get("name", "")), str(body.get("password", "")))
            except AuthError as exc:
                self._json({"ok": False, "msg": str(exc)}, exc.status)
                return
            with runner.lock:
                runner.ops.record({"t": round(runner.sim.now, 1), "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "operator": sess.name, "cmd": "login", "args": {"role": sess.role},
                                   "ok": True, "msg": "로그인"})
            self._json({"ok": True, "name": sess.name, "role": sess.role, "csrf": sess.csrf, "auth": True},
                       headers={"Set-Cookie": session_cookie(token, secure_cookie)})

        def _control(self, body: dict, sess: Session) -> None:
            action = body.get("action")
            if not getattr(runner, "simulated", True):
                self._json({"ok": False, "msg": "실운영 관제에는 시뮬레이션 제어(배속·재시작)가 없습니다"}, 400)
                return
            try:
                if action == "speed":
                    value = float(body.get("value", 1.0))
                    if value not in SPEEDS:
                        raise ValueError(f"속도는 {SPEEDS} 중 하나")
                    runner.speed = value
                elif action == "pause":
                    runner.paused = bool(body.get("value", True))
                elif action == "auto_operator":
                    runner.set_auto_operator(bool(body.get("value", False)))
                elif action == "reset":
                    scenario = str(body.get("scenario", runner.sim.scenario.name))
                    if scenario not in PRESETS:
                        raise ValueError("알 수 없는 시나리오")
                    runner.reset(scenario, int(body.get("seed", runner.sim.seed)),
                                 str(body.get("strategy", runner.sim.manager.cfg.strategy)))
                    with runner.lock:
                        runner.ops.record({"t": 0.0, "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "operator": sess.name, "cmd": "reset",
                                           "args": {"scenario": scenario}, "ok": True, "msg": "시뮬레이션 재시작"})
                else:
                    raise ValueError("알 수 없는 제어")
                runner._snap = None
                self._json({"ok": True})
            except ValueError as exc:
                self._json({"ok": False, "msg": str(exc)}, 400)

    return Handler


def account_commands(args, store: Store) -> int | None:
    """--add-user / --create-api-key: change accounts and exit (no server)."""
    auth = Auth(store)
    if args.add_user:
        name, _, role = args.add_user.partition(":")
        password = os.environ.get("FCT_NEW_PASSWORD") or getpass.getpass(f"{name} 비밀번호(10자 이상): ")
        auth.create_user(name, password, role or "operator")
        print(f"계정 {name} ({role or 'operator'}) 저장: {store.path}")
        return 0
    if args.create_api_key:
        key = auth.create_api_key(args.create_api_key)
        print(f"WMS API 키({args.create_api_key}) — 한 번만 표시합니다:\n{key}")
        return 0
    return None


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AMR 관제탑 웹 서버")
    ap.add_argument("--scenario", default="standard", choices=sorted(PRESETS))
    ap.add_argument("--strategy", default="locks+resolve", choices=STRATEGIES)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--speed", type=float, default=1.0, choices=SPEEDS)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="계정·세션·감사 로그·WMS 멱등 키 저장소(SQLite)")
    ap.add_argument("--no-auth", action="store_true", help="개발용: 인증 끄기(루프백 바인딩에서만 허용)")
    ap.add_argument("--add-user", metavar="NAME:ROLE", help="계정 추가 후 종료(ROLE: viewer|operator|supervisor)")
    ap.add_argument("--create-api-key", metavar="NAME", help="WMS API 키 발급 후 종료")
    ap.add_argument("--read-only", action="store_true", help="관찰 전용: 모든 명령 거부")
    ap.add_argument("--auto-operator", action="store_true", help="가상 운영자가 SOP대로 알람에 대응")
    ap.add_argument("--jdamr-url", default=DEFAULT_URL, help="실물 JD-AMR 읽기 전용 상태 URL (루프백만)")
    ap.add_argument("--no-jdamr", action="store_true")
    args = ap.parse_args(argv)

    store = Store(args.db)
    done = account_commands(args, store)
    if done is not None:
        return done
    if args.no_auth and not is_loopback(args.host):
        print("--no-auth는 루프백 주소에서만 쓸 수 있습니다", flush=True)
        return 2
    auth = None if args.no_auth else Auth(store)
    if auth is not None:
        generated = auth.bootstrap(os.environ.get("FCT_ADMIN_PASSWORD"))
        if generated:
            print(f"첫 실행: 감독자 계정 admin을 만들었습니다. 비밀번호(한 번만 표시): {generated}", flush=True)
    runner = Runner(args.scenario, args.seed, args.strategy, args.speed, args.auto_operator, args.read_only, store)
    problems = runner.sim.layout.check()          # refuse to run on a broken site (unreachable nodes, classes)
    if problems:
        print("레이아웃 검증 실패:\n  " + "\n  ".join(problems[:10]), flush=True)
        return 2
    jdamr = None if args.no_jdamr else JdamrReadOnly(args.jdamr_url)
    runner.start()
    if jdamr:
        jdamr.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runner, jdamr, auth))
    server.daemon_threads = True
    print(f"AMR 관제탑: http://{args.host}:{args.port}  (시나리오 {args.scenario}, 전략 {args.strategy}"
          f"{', 관찰 전용' if args.read_only else ''}{', 인증 꺼짐(개발용)' if auth is None else ''})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        if jdamr:
            jdamr.stop()
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
