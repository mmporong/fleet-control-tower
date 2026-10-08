"""Fleet manager service: the control core against a real MQTT broker, in real time.

The same ``FleetManager`` and ``Dispatcher`` the simulation benchmarks run
here as a service. What differs from the simulation:

- vehicles are other processes (``fleet.agvsim``, a vendor simulator, real
  vehicles) on an MQTT broker; every inbound VDA 5050 message is checked
  against the official JSON schemas (``fleet.schema``) and a malformed one is
  dropped and counted, never allowed to stop the control loop;
- the layout comes from a VDMA LIF file and its sidecar (``fleet.lif``);
- tasks, the WMS outbox, idempotency keys, per-vehicle order snapshots and
  the audit log live in SQLite (``fleet.store``). After a restart the manager
  starts in RESYNC: it holds the reservations of the last snapshot, waits for
  each vehicle's state, keeps orders the vehicles are still driving and
  assigns nothing new until that is done;
- order and action ids carry a run id, so a restarted manager never reuses an
  id a vehicle has already seen;
- an exception inside one control cycle is logged as an event and an alarm,
  the fleet is put on HOLD (vehicles stop at their base ends), and the loop
  goes on.

    python3 -m fleet.fms --broker 127.0.0.1:1883 --site sites/dc.lif.json --port 8096
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
from pathlib import Path
import random
import secrets
import threading
import time
import traceback
from types import SimpleNamespace

from .clock import WallClock
from .control import FleetManager, ManagerConfig
from .dispatcher import Dispatcher, task_to_json
from .layout import Layout
from .mqtt import MqttClient, Will
from .mqttbus import MqttBus
from .ops import Operations
from .process import TransportProcess
from .schema import validate_message
from .store import Store

log = logging.getLogger(__name__)
TICK_S = 0.2
FMS_TOPIC = "fleet/v1/fms/state"


class Runtime:
    """The parts of ``Simulation`` the operations layer and web server use, backed by the real fleet."""

    simulated = False

    def __init__(self, layout: Layout, host: str, port: int, store: Store, clock: WallClock | None = None,
                 config: ManagerConfig | None = None, tls=None, run_id: str | None = None,
                 resync_s: float = 15.0) -> None:
        self.layout = layout
        self.store = store
        self.clock = clock or WallClock()
        self.run_id = run_id or secrets.token_hex(3)
        self.scenario = SimpleNamespace(name="runtime", title="실운영(MQTT)")
        self.seed = 0
        will = json.dumps({"state": "OFFLINE", "run": self.run_id}).encode()
        self.client = MqttClient(host, port, f"fms-{self.run_id}", keepalive=15, tls=tls,
                                 will=Will(FMS_TOPIC, will, 1, True))
        self.bus = MqttBus(self.client, self.clock)
        self.manager = FleetManager(layout, self.bus, config or ManagerConfig(), clock=self.clock,
                                    validator=validate_message)
        self.manager.run_id = f"{self.run_id}-"
        self.dispatcher = Dispatcher(self.manager, random.Random(), run_id=f"{self.run_id}-")
        self.process = TransportProcess(self.dispatcher, random.Random())
        self.dispatcher.process = self.process
        self.dispatcher.sinks.append(self._persist_event)
        self.incidents: list = []
        self.handovers: list = []
        self.errors = 0
        self._snap_hash: dict[str, str] = {}
        self._next_save = 0.0
        # durable state from the previous run; the manager's clock must read the real time before it restores
        self.now = self.manager.now = self.clock.now()
        restored = self.dispatcher.restore(store.open_tasks(), store.last_event_seq())
        snapshots = store.robots()
        if snapshots or restored:
            self.manager.restore(snapshots, resync_s)
        self.client.on_connect = lambda: self.bus.publish(
            FMS_TOPIC, {"state": "ONLINE", "run": self.run_id}, self.clock.now())

    # ------------------------------------------------------------ control loop
    def start(self) -> None:
        self.client.start()

    def step(self) -> None:
        now = self.clock.now()
        dt = max(0.0, now - self.now)
        self.now = now
        try:
            self.manager.now = now          # handlers stamp arrivals with the manager's clock: set it first
            self.bus.deliver_due(now)
            self.manager.tick(now, dt or TICK_S)
            self.dispatcher.tick(now)
        except Exception as exc:                      # a bug must not stop the control room
            self.errors += 1
            log.error("control cycle failed:\n%s", traceback.format_exc())
            self.manager.event("internal_error", "fms", f"관제 주기 오류: {type(exc).__name__}: {exc}", "crit")
            self.manager.mode = "HOLD"
            self.manager.alarms.set("FMS_INTERNAL_ERROR", "fms", True, now, f"{type(exc).__name__}: {exc}"[:200])
        if now >= self._next_save:
            self._next_save = now + 0.5
            self._save_snapshots()

    def stop(self) -> None:
        self._save_snapshots()
        if self.client.connected:
            self.bus.publish(FMS_TOPIC, {"state": "OFFLINE", "run": self.run_id}, self.clock.now())
            self.client.disconnect()
        self.client.stop()

    # ------------------------------------------------------------ persistence
    def _persist_event(self, ev: dict, task) -> None:
        self.store.append_event(ev)
        self.store.save_task(task_to_json(task))

    def _save_snapshots(self) -> None:
        for rec in self.manager.robots.values():
            snap = self.manager.robot_snapshot(rec)
            text = json.dumps(snap, sort_keys=True)
            if self._snap_hash.get(rec.serial) != text:
                self._snap_hash[rec.serial] = text
                self.store.save_robot(rec.serial, snap)

    # ------------------------------------------------------------ the Simulation surface used elsewhere
    def kpi(self) -> dict:
        m = self.manager
        stats = self.dispatcher.stats(self.now)
        busy = sum(r.busy_s for r in m.robots.values())
        wait = sum(r.wait_s for r in m.robots.values())
        hours = max(self.now / 3600.0, 1e-6)
        return {**stats, "robots": len(m.robots),
                "online": sum(1 for r in m.robots.values() if r.connection == "ONLINE"),
                "working": sum(1 for r in m.robots.values() if r.task_id),
                "traffic_wait_ratio": round(wait / busy, 3) if busy > 0 else 0.0,
                "deadlocks": m.counters["deadlocks"], "deadlocks_resolved": m.counters["deadlocks_resolved"],
                "protective_stops": m.counters["protective_stops"], "near_miss_fms": m.counters["near_miss"],
                # ground truth does not exist outside the simulation
                "close_calls": None, "clearance_violations": None, "contacts": None, "tracking_ratio": None,
                "invalid_msgs": m.counters["invalid_msgs"], "comms_lost": m.counters["comms_lost"],
                "alarms_per_h": round(m.alarms.total_activations / hours, 1), "internal_errors": self.errors,
                "sim_hours": round(hours, 3)}

    def incident(self, inc_id: str) -> dict:
        raise KeyError(inc_id)

    def ack_incident(self, key: str) -> None:
        return None

    def _sim_only(self, *_a, **_k):
        raise ValueError("훈련 주입은 시뮬레이션 전용입니다")

    drill_fault = drill_comms = drill_door = drill_intruder = door_field_fix = _sim_only

    def snapshot(self) -> dict:
        m = self.manager
        locks_n, locks_l = {}, {}
        for res, owner in m.locks.holder.items():
            (locks_n if res.startswith("N:") else locks_l)[res[2:]] = owner
        return {
            "t": round(self.now, 1), "scenario": "runtime", "scenario_title": self.scenario.title,
            "layout_key": "runtime", "strategy": m.cfg.strategy, "mode": m.mode, "seed": 0,
            "robots": [{**m.robot_view(r), "gaps": m.capability_gaps(r)} for r in m.robots.values()],
            "truth": {"robots": [], "humans": [], "forklifts": [], "external": []},
            "tracks": [[tr.id, tr.cls, round(tr.x, 2), round(tr.y, 2), round(tr.vx, 2), round(tr.vy, 2),
                        round(tr.sigma(self.now), 2), round(self.now - tr.last_seen, 1)]
                       for tr in m.tracker.tracks.values()],
            "external": [], "locks": locks_n, "lanes": locks_l,
            "blocks": {k: list(v) for k, v in m.blocks.items()},
            "doors": m.doors.views(self.now) if m.doors is not None else [],
            "zones": [{"id": z.id, "locked": z.locked, "limit": m.active_zone_limits().get(z.id),
                       "override": m.zone_speed_override.get(z.id)} for z in self.layout.zones.values()],
            "alarms": m.alarms.active_list(), "alarm_counts": m.alarms.counts(), "kpi": self.kpi(),
            "tasks": self.dispatcher.queue_view(self.now), "events": list(m.events)[-80:],
            "deadlocks": [{"members": v["members"], "since": round(v["start"], 1), "attempts": v["attempts"],
                           "method": v.get("method")} for v in m.deadlocks.values()],
            "incidents": [], "heat": {k: round(v, 1) for k, v in sorted(m.congestion.items(),
                                                                         key=lambda kv: -kv[1])[:40]},
            "bus": dict(self.bus.stats), "links_down": [], "paused": sorted(m.robot_paused | m.area_paused),
            "task_rate": 0.0, "site": self.layout.site, "site_state": {}, "task_kinds": self.process.task_kinds,
            "runtime": {"run_id": self.run_id, "broker": f"{self.client.host}:{self.client.port}",
                        "broker_connected": self.client.connected, "errors": self.errors},
        }


class RuntimeRunner(threading.Thread):
    """Drives the runtime at a fixed rate and offers what ``fleet.server.make_handler`` reads from a runner."""

    simulated = False

    def __init__(self, runtime: Runtime, store: Store, read_only: bool = False) -> None:
        super().__init__(daemon=True, name="fms-runtime")
        from .server import IdemTable
        self.sim = runtime
        self.store = store
        self.read_only = read_only
        self.lock = threading.RLock()
        self.ops = Operations(runtime, read_only=read_only, store=store)
        self.wms_seen = IdemTable(store)
        self.history: deque = deque(maxlen=720)
        self.speed, self.paused, self.auto_operator, self.lag_s = 1.0, False, False, 0.0
        self._halt = threading.Event()
        self._snap: dict | None = None
        self._snap_at = 0.0

    def run(self) -> None:
        self.sim.start()
        while not self._halt.wait(TICK_S / max(1e-6, self.sim.clock.scale)):
            with self.lock:
                self.sim.step()

    def stop(self) -> None:
        self._halt.set()
        with self.lock:
            self.sim.stop()

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            if self._snap is None or now - self._snap_at > 0.15:
                snap = self.sim.snapshot()
                snap["run"] = {"speed": 1.0, "effective_speed": 1.0, "paused": False, "lag_s": 0.0,
                               "auto_operator": False, "read_only": self.read_only,
                               "interventions": self.ops.counts["interventions"], "simulated": False}
                snap["audit"] = list(self.ops.audit)[-40:]
                self._snap, self._snap_at = snap, now
            return self._snap

    def local_view(self, serial: str) -> dict:
        with self.lock:
            m = self.sim.manager
            rec = m.robots[serial]
            return {"robot": m.robot_view(rec), "robots": [], "tracks": [], "detections": rec.detections,
                    "external": [], "racks": [], "field": {"state": rec.safety_field, "cause": rec.field_cause}}

    def set_auto_operator(self, on: bool) -> None:
        raise ValueError("실운영 관제에는 가상 운영자가 없습니다")


def load_site(site: str, extras: str | None) -> Layout:
    from .lif import import_lif
    path = Path(site)
    extras_path = Path(extras) if extras else path.with_name(path.name.replace(".lif.json", ".site.json"))
    return import_lif(json.loads(path.read_text()), json.loads(extras_path.read_text()))


def main(argv: list[str] | None = None) -> int:
    from http.server import ThreadingHTTPServer
    from .auth import Auth
    from .server import account_commands, is_loopback, make_handler
    ap = argparse.ArgumentParser(description="AMR 관제 서비스(MQTT·VDA 5050)")
    ap.add_argument("--broker", default="127.0.0.1:1883")
    ap.add_argument("--site", required=True, help="VDMA LIF 파일 (예: sites/dc.lif.json)")
    ap.add_argument("--extras", help="사이드카(기본: <site>에서 .lif.json → .site.json)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--db", default=str(Path.home() / ".local" / "state" / "fleet-control-tower" / "fms.sqlite3"))
    ap.add_argument("--no-auth", action="store_true", help="개발용: 인증 끄기(루프백 바인딩에서만 허용)")
    ap.add_argument("--add-user", metavar="NAME:ROLE")
    ap.add_argument("--create-api-key", metavar="NAME")
    ap.add_argument("--read-only", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(args.db)
    done = account_commands(args, store)
    if done is not None:
        return done
    if args.no_auth and not is_loopback(args.host):
        print("--no-auth는 루프백 주소에서만 쓸 수 있습니다", flush=True)
        return 2
    layout = load_site(args.site, args.extras)
    problems = layout.check()
    if problems:
        print("레이아웃 검증 실패:\n  " + "\n  ".join(problems[:10]), flush=True)
        return 2
    auth = None if args.no_auth else Auth(store)
    if auth is not None:
        import os
        generated = auth.bootstrap(os.environ.get("FCT_ADMIN_PASSWORD"))
        if generated:
            print(f"첫 실행: 감독자 계정 admin을 만들었습니다. 비밀번호(한 번만 표시): {generated}", flush=True)
    host, _, port = args.broker.partition(":")
    runtime = Runtime(layout, host, int(port or 1883), store)
    runner = RuntimeRunner(runtime, store, read_only=args.read_only)
    runner.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runner, None, auth))
    server.daemon_threads = True
    print(f"관제 서비스: http://{args.host}:{args.port} · 브로커 {args.broker} · 실행 ID {runtime.run_id}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
