"""Control room web server: runs the simulation in real time and serves the UI.

Standard library only. Binds to loopback by default. Commands go through
``Operations`` (validated + audited); ``--read-only`` turns the whole server
into an observation console.

    python3 -m fleet.server --scenario standard --port 8095
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
from pathlib import Path
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .autoop import AutoOperator
from .control import STRATEGIES
from .jdamr import DEFAULT_URL, JdamrReadOnly
from .ops import Operations
from .scenario import PRESETS, get
from .sim import Simulation

WEB = Path(__file__).resolve().parents[1] / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/logic.js": ("logic.js", "text/javascript; charset=utf-8")}
MAX_BODY = 64 * 1024
SPEEDS = (0.5, 1.0, 2.0, 5.0, 10.0)


class Runner(threading.Thread):
    """Advances the simulation against wall time and keeps a snapshot cache."""

    def __init__(self, scenario: str, seed: int, strategy: str, speed: float, auto_operator: bool,
                 read_only: bool) -> None:
        super().__init__(daemon=True, name="sim-runner")
        self.lock = threading.RLock()
        self.read_only = read_only
        self.auto_operator = auto_operator
        self.speed = speed
        self.paused = False
        self.lag_s = 0.0
        self._stop = threading.Event()
        self._snap: dict | None = None
        self._snap_at = 0.0
        self.wms_seen: dict = {}
        self.reset(scenario, seed, strategy)

    def reset(self, scenario: str, seed: int, strategy: str) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
        with self.lock:
            prev_audit = list(self.ops.audit) if hasattr(self, "ops") else []
            self.sim = Simulation(get(scenario), seed=seed, strategy=strategy)
            self.ops = Operations(self.sim, read_only=self.read_only)
            self.ops.audit.extend(prev_audit[-200:])
            self.autoop = AutoOperator(self.ops, random.Random(seed + 99))
            if self.auto_operator:
                self.sim.tick_hooks.append(self.autoop.tick)
            self.history: deque = deque(maxlen=720)
            self._next_sample = 0.0
            self.wms_seen = {}
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


def make_handler(runner: Runner, jdamr: JdamrReadOnly | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FleetControlTower/0.1"

        def log_message(self, fmt: str, *args) -> None:   # keep the console quiet
            return

        # ---------------------------------------------------------- helpers
        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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

        def _operator(self) -> str:
            name = unquote((self.headers.get("X-Operator") or "operator").strip())
            return "".join(ch for ch in name if ch.isprintable())[:40] or "operator"

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
            elif path == "/api/layout":
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
            try:
                while True:
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
            if path == "/api/command":
                if runner.read_only:
                    with runner.lock:
                        res = runner.ops.execute(body, self._operator())
                    self._json(res, 403)
                    return
                with runner.lock:
                    res = runner.ops.execute(body, self._operator())
                    runner._snap = None
                self._json(res, 200 if res["ok"] else 400)
            elif path == "/api/wms/orders":
                if runner.read_only:
                    self._json({"ok": False, "msg": "관찰 전용 모드"}, 403)
                    return
                try:
                    with runner.lock:
                        res = runner.ops.submit_wms_order(body, runner.wms_seen)
                    self._json(res, 200 if res.get("duplicate") else 201)
                except ValueError as exc:
                    self._json({"ok": False, "msg": str(exc)}, 400 if "이미" not in str(exc) else 409)
            elif path == "/api/control":
                if runner.read_only:
                    self._json({"ok": False, "msg": "관찰 전용 모드"}, 403)
                    return
                self._control(body)
            else:
                self._json({"error": "not found"}, 404)

        def _control(self, body: dict) -> None:
            action = body.get("action")
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
                        runner.ops.audit.append({"t": 0.0, "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                 "operator": self._operator(), "cmd": "reset",
                                                 "args": {"scenario": scenario}, "ok": True, "msg": "시뮬레이션 재시작"})
                else:
                    raise ValueError("알 수 없는 제어")
                runner._snap = None
                self._json({"ok": True})
            except ValueError as exc:
                self._json({"ok": False, "msg": str(exc)}, 400)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AMR 관제탑 웹 서버")
    ap.add_argument("--scenario", default="standard", choices=sorted(PRESETS))
    ap.add_argument("--strategy", default="locks+resolve", choices=STRATEGIES)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--speed", type=float, default=1.0, choices=SPEEDS)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--read-only", action="store_true", help="관찰 전용: 모든 명령 거부")
    ap.add_argument("--auto-operator", action="store_true", help="가상 운영자가 SOP대로 알람에 대응")
    ap.add_argument("--jdamr-url", default=DEFAULT_URL, help="실물 JD-AMR 읽기 전용 상태 URL (루프백만)")
    ap.add_argument("--no-jdamr", action="store_true")
    args = ap.parse_args(argv)

    runner = Runner(args.scenario, args.seed, args.strategy, args.speed, args.auto_operator, args.read_only)
    problems = runner.sim.layout.check()          # refuse to run on a broken site (unreachable nodes, classes)
    if problems:
        print("레이아웃 검증 실패:\n  " + "\n  ".join(problems[:10]), flush=True)
        return 2
    jdamr = None if args.no_jdamr else JdamrReadOnly(args.jdamr_url)
    runner.start()
    if jdamr:
        jdamr.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runner, jdamr))
    server.daemon_threads = True
    print(f"AMR 관제탑: http://{args.host}:{args.port}  (시나리오 {args.scenario}, 전략 {args.strategy}"
          f"{', 관찰 전용' if args.read_only else ''})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        if jdamr:
            jdamr.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
