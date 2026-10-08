"""The fleet manager as a service over a real MQTT broker (the in-process test broker), with simulated
vehicles as separate MQTT clients: tasks, Last Will, a manager restart with resync, malformed messages."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from fleet.agvsim import AgvSim
from fleet.clock import WallClock
from fleet.fms import Runtime, RuntimeRunner, load_site
from fleet.layout import warehouse_layout
from fleet.minibroker import MiniBroker
from fleet.mqtt import MqttClient
from fleet.store import Store

SCALE = 4.0          # manager and vehicles share one clock running four times faster than real time


def wait_for(cond, limit: float, step: float = 0.05) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.broker = MiniBroker()
        self.broker.start()
        self.port = self.broker.port
        self.layout = warehouse_layout()
        self.clock = WallClock(scale=SCALE)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "fms.sqlite3"
        self.runners: list[RuntimeRunner] = []
        self.sims: list[AgvSim] = []

    def tearDown(self):
        for r in self.runners:
            r.stop()
        for s in self.sims:
            s.stop()
        self.broker.stop()
        self.tmp.cleanup()

    def fms(self, run_id: str) -> RuntimeRunner:
        store = Store(self.db)
        rt = Runtime(self.layout, "127.0.0.1", self.port, store, clock=self.clock, run_id=run_id, resync_s=8.0)
        runner = RuntimeRunner(rt, store)
        runner.start()
        self.runners.append(runner)
        return runner

    def vehicles(self, specs) -> AgvSim:
        sim = AgvSim(self.layout, specs, "127.0.0.1", self.port, clock=self.clock)
        sim.start()
        self.sims.append(sim)
        return sim

    def test_tasks_complete_over_mqtt_with_schema_checked_messages(self):
        runner = self.fms("r1")
        self.vehicles([("AMR-01", "GENERIC", "P01"), ("AMR-02", "GENERIC", "P02")])
        m = runner.sim.manager
        self.assertTrue(wait_for(lambda: all(r.factsheet for r in m.robots.values()) and len(m.robots) == 2, 10))
        with runner.lock:
            t1 = runner.sim.dispatcher.add_task("TRANSPORT", "C3", "C7", ref="W-1")
            t2 = runner.sim.dispatcher.add_task("MOVE", "F2", None)
        done = lambda: all(t.id not in runner.sim.dispatcher.tasks for t in (t1, t2))      # noqa: E731
        self.assertTrue(wait_for(done, 40), [t.to_json(0) for t in runner.sim.dispatcher.tasks.values()])
        self.assertEqual({t.state for t in (t1, t2)}, {"DONE"})
        self.assertEqual(m.counters["invalid_msgs"], 0)
        self.assertEqual(m.locks.check(), [])
        kinds = [e["type"] for e in runner.store.events_after(0) if e["task"] == t1.id]
        self.assertEqual(kinds, ["CREATED", "ASSIGNED", "STOP_DONE", "STOP_DONE", "COMPLETED"])
        self.assertTrue(all(o.startswith("AMR-0") and "-r1-O" in o for o in [r.order_id for r in m.robots.values()
                                                                             if r.order_id]))

    def test_last_will_marks_a_crashed_vehicle_and_it_comes_back(self):
        runner = self.fms("r1")
        sim = self.vehicles([("AMR-01", "GENERIC", "P01")])
        m = runner.sim.manager
        self.assertTrue(wait_for(lambda: "AMR-01" in m.robots and m.robots["AMR-01"].connection == "ONLINE", 10))
        sim.crash("AMR-01")
        self.assertTrue(wait_for(lambda: m.robots["AMR-01"].connection == "CONNECTIONBROKEN", 10))
        # the retained Will is what a client connecting later sees as well
        seen = []
        probe = MqttClient("127.0.0.1", self.port, "probe")
        probe.on_message = lambda t, p, r: seen.append(json.loads(p)["connectionState"])
        probe.connect()
        probe.start()
        probe.subscribe("uagv/v2/sim/AMR-01/connection", qos=1)
        self.assertTrue(wait_for(lambda: "CONNECTIONBROKEN" in seen, 5))
        probe.disconnect()
        probe.stop()
        self.vehicles([("AMR-01", "GENERIC", "P01")])
        self.assertTrue(wait_for(lambda: m.robots["AMR-01"].connection == "ONLINE", 10))

    def test_restart_resyncs_and_the_task_finishes_without_double_booking(self):
        first = self.fms("r1")
        self.vehicles([("AMR-01", "GENERIC", "P01"), ("AMR-02", "GENERIC", "P10")])
        m1 = first.sim.manager
        self.assertTrue(wait_for(lambda: len(m1.robots) == 2 and all(r.factsheet for r in m1.robots.values()), 10))
        with first.lock:
            task = first.sim.dispatcher.add_task("TRANSPORT", "C3", "H7", ref="W-9")
        # restart while the vehicle is on its way (after the first stop, so a load is on board)
        self.assertTrue(wait_for(lambda: task.started_at is not None, 40))
        robot = task.robot
        first.stop()
        self.runners.remove(first)
        second = self.fms("r2")
        m2 = second.sim.manager
        self.assertEqual(m2.mode, "RESYNC")
        self.assertIn(task.id, second.sim.dispatcher.tasks)            # the task came back from the store
        self.assertTrue(m2.locks.resources_of(robot))                 # its reservations are held before any state
        self.assertTrue(wait_for(lambda: m2.mode == "RUNNING", 20))
        self.assertTrue(wait_for(lambda: task.id not in second.sim.dispatcher.tasks, 60),
                        [e["msg"] for e in list(m2.events)[-10:]])
        final = second.store.task(task.id)
        self.assertEqual(final["state"], "DONE", final["note"])
        self.assertEqual(m2.locks.check(), [])
        self.assertEqual(m2.counters["mismatch"], 0)
        # the vehicle kept the first run's order and the restarted manager adopted it
        self.assertTrue(any(e["type"] == "resync_ok" and e["source"] == robot for e in m2.events),
                        [e["msg"] for e in m2.events])
        # a new order from the restarted manager carries its own run id, so no id the vehicle saw comes back
        with second.lock:
            nxt = second.sim.dispatcher.add_task("MOVE", "B3", None)
        self.assertTrue(wait_for(lambda: nxt.robot is not None, 20))
        self.assertIn("-r2-O", m2.robots[nxt.robot].order_id)
        self.assertTrue(wait_for(lambda: nxt.id not in second.sim.dispatcher.tasks, 40))

    def test_restart_while_driving_keeps_the_released_base(self):
        first = self.fms("r1")
        sim = self.vehicles([("AMR-01", "GENERIC", "P01")])
        m1 = first.sim.manager
        self.assertTrue(wait_for(lambda: "AMR-01" in m1.robots and m1.robots["AMR-01"].factsheet, 10))
        with first.lock:
            task = first.sim.dispatcher.add_task("MOVE", "J8", None)
        rec1 = m1.robots["AMR-01"]
        driving = lambda: rec1.driving and rec1.state_order_id == rec1.order_id and rec1.base_end > rec1.cur_idx  # noqa
        self.assertTrue(wait_for(driving, 30))
        order = rec1.order_id
        first.stop()
        self.runners.remove(first)
        v = sim.clients["AMR-01"].v
        second = self.fms("r2")
        m2 = second.sim.manager
        held = {r[2:] for r in m2.locks.resources_of("AMR-01") if r.startswith("N:")}
        self.assertTrue(held)
        # what the vehicle may still drive (its released nodes) is reserved before it has said anything
        released = [n["nodeId"] for n in v.nodes if n["released"]]
        self.assertTrue(set(released) <= held | {v.last_node_id}, (released, held))
        self.assertTrue(wait_for(lambda: any(e["type"] == "resync_ok" for e in m2.events), 20))
        self.assertEqual(m2.robots["AMR-01"].order_id, order)            # the same order, continued
        self.assertTrue(wait_for(lambda: task.id not in second.sim.dispatcher.tasks, 60))
        self.assertEqual(second.store.task(task.id)["state"], "DONE")
        self.assertEqual(m2.locks.check(), [])
        # the WMS reads the outbox through the restart: the first run's events are still there, in order
        from http.server import ThreadingHTTPServer
        import threading
        from urllib.request import urlopen
        from fleet.server import make_handler
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(second, None, None))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/wms/events?after=0", timeout=5) as resp:
                events = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        kinds = [e["type"] for e in events if e["task"] == task.id]
        self.assertEqual(kinds[:2], ["CREATED", "ASSIGNED"])
        self.assertEqual(kinds[-1], "COMPLETED")
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))

    def test_malformed_messages_are_dropped_not_fatal(self):
        runner = self.fms("r1")
        self.vehicles([("AMR-01", "GENERIC", "P01")])
        m = runner.sim.manager
        self.assertTrue(wait_for(lambda: "AMR-01" in m.robots and m.robots["AMR-01"].has_state, 10))
        bad = MqttClient("127.0.0.1", self.port, "bad-vendor")
        bad.connect()
        bad.start()
        bad.publish("uagv/v2/acme/X-1/state", b'{"headerId": "not-a-number"}', qos=0)
        bad.publish("uagv/v2/acme/X-1/state", b"not json at all", qos=0)
        self.assertTrue(wait_for(lambda: m.counters["invalid_msgs"] >= 1 and runner.sim.bus.stats["invalid_json"] >= 1,
                                 5))
        bad.disconnect()
        bad.stop()
        self.assertEqual(runner.sim.errors, 0)
        self.assertTrue(m.robots["AMR-01"].connection == "ONLINE")



class RuntimeDoorTests(unittest.TestCase):
    """The distribution centre from its LIF file: forklifts cross the fire wall through rapid-roll doors."""

    def setUp(self):
        self.broker = MiniBroker()
        self.broker.start()
        self.layout = load_site(str(Path(__file__).resolve().parents[1] / "sites" / "dc.lif.json"), None)
        self.clock = WallClock(scale=8.0)
        self.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(self.tmp.name) / "fms.sqlite3")
        self.runner = RuntimeRunner(Runtime(self.layout, "127.0.0.1", self.broker.port, store, clock=self.clock,
                                            run_id="d1"), store)
        self.sim = None

    def tearDown(self):
        self.runner.stop()
        if self.sim is not None:
            self.sim.stop()
        self.broker.stop()
        self.tmp.cleanup()

    def start(self, doors: bool) -> None:
        self.runner.start()
        self.sim = AgvSim(self.layout, [("AGF-01", "FORKLIFT", "PA39_5")], "127.0.0.1", self.broker.port,
                          clock=self.clock, doors=doors)
        self.sim.start()

    def test_a_forklift_crosses_the_fire_wall_through_a_door_that_answers(self):
        self.start(doors=True)
        m = self.runner.sim.manager
        self.assertTrue(wait_for(lambda: "AGF-01" in m.robots and m.robots["AGF-01"].factsheet, 10))
        with self.runner.lock:
            task = self.runner.sim.dispatcher.add_task("MOVE", "NE54", None, ref="W-DOOR")
        self.assertTrue(wait_for(lambda: task.id not in self.runner.sim.dispatcher.tasks, 60),
                        [e["msg"] for e in m.events][-8:])
        self.assertEqual(task.state, "DONE")
        self.assertGreaterEqual(m.doors.view["D-N"].opens, 1)
        self.assertFalse([a for a in m.alarms.active_list() if a["type"] == "FACILITY_FAULT"])

    def test_a_door_without_a_controller_is_kept_out_of_the_routes(self):
        self.start(doors=False)
        m = self.runner.sim.manager
        self.assertTrue(wait_for(lambda: m.doors.view["D-N"].fail_cause == "stale", 10))
        self.assertTrue(any("상태 보고 없음" in e["msg"] for e in m.events if e["type"] == "door_fault"))
        self.assertTrue({"NE42_6|NE47", "NW42_6|NW47"} <= m.doors.unavailable_lanes(m.now))

if __name__ == "__main__":
    unittest.main()
