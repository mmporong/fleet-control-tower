"""Regression tests for the second review round: each test fails on the code before its fix."""
from __future__ import annotations

from http.client import HTTPConnection
import json
import math
from pathlib import Path
import random
import tempfile
import time
import unittest

from fleet.auth import Auth, AuthError
from fleet.bus import Bus
from fleet.control import FleetManager
from fleet.facility import DoorSpec, DoorSupervisor
from fleet.fms import Runtime
from fleet.layout import warehouse_layout
from fleet.mqttbus import qos_retain
from fleet.ops import Operations
from fleet.perception import Detection, Tracker
from fleet.server import Runner
from fleet.store import Store, chain_hash
from fleet.traffic import node_res
from fleet.vehicle import Vehicle
from tests.test_readiness import quiet_dc
from tests.test_server import Client, serve


def confirmed_track(manager, cls: str, x: float, y: float) -> str:
    """A confirmed, standing track of ``cls`` at (x, y) in the manager's tracker; returns its id."""
    manager.tracker = Tracker()
    for dt in (-0.5, 0.0):
        manager.tracker.update([Detection(cls, x, y, "infra")], manager.now + dt)
    (tid,) = manager.tracker.tracks
    return tid


class AisleRequestTests(unittest.TestCase):
    def setUp(self):
        self.sim = quiet_dc()
        self.m = self.sim.manager
        self.aisle = self.sim.layout.zones["Z-R1"]
        xs = [p[0] for p in self.aisle.polygon]
        ys = [p[1] for p in self.aisle.polygon]
        self.cx, self.y0, self.y1 = (min(xs) + max(xs)) / 2, min(ys), max(ys)
        self.nodes = self.m._zone_nodes["Z-R1"]

    def truck_waiting_at(self, x: float, y: float) -> str:
        tid = confirmed_track(self.m, "forklift", x, y)
        self.m._fl_still_since = {tid: self.m.now - 5.0}
        self.m._update_blocks()
        return tid

    def test_a_robot_inside_the_aisle_may_still_leave(self):
        self.truck_waiting_at(self.cx, self.y0 - 2.0)        # in front of the south end
        self.assertTrue(all(node_res(n) in self.m.aisle_requests for n in self.nodes))
        rec = self.m.robots["AGF-01"]
        inside = self.sim.layout.nodes[self.nodes[0]]
        self.m.locks.force(rec.serial, node_res(inside.id))
        rec.x, rec.y = inside.x, inside.y
        self.assertIsNone(self.m._block_for(rec, node_res(self.nodes[-1])))   # its way out is not closed
        self.m.locks.release(rec.serial, [node_res(inside.id)])
        rec.x, rec.y = self.cx + 30.0, self.y0 - 3.0                            # a robot outside is held out
        self.assertEqual(self.m._block_for(rec, node_res(self.nodes[0]))[0], "forklift")

    def test_a_truck_beside_an_aisle_is_not_at_its_mouth(self):
        self.assertIsNone(self.m._aisle_at_mouth(self.cx + 3.0, self.y0 - 2.0))     # between two aisles
        self.assertIsNotNone(self.m._aisle_at_mouth(self.cx, self.y1 + 2.0))         # north mouth

    def test_an_unconfirmed_truck_does_not_close_an_aisle(self):
        self.m.tracker = Tracker()
        self.m.tracker.update([Detection("forklift", self.cx, (self.y0 + self.y1) / 2, "infra")], self.m.now)
        self.m._update_blocks()
        self.assertFalse(any(node_res(n) in self.m.blocks for n in self.nodes))


class StoppedTruckTests(unittest.TestCase):
    def test_a_truck_waiting_short_of_an_intersection_gets_its_turn_circle(self):
        sim = quiet_dc()
        m = sim.manager
        node = sim.layout.nodes["NE22_8"]                   # the R2 corner on the north main aisle
        tid = confirmed_track(m, "forklift", node.x, node.y - 3.0)      # backed off 3 m into the aisle mouth
        m.tracker.tracks[tid].heading = -math.pi / 2                     # its last motion: backing off
        m._fl_still_since = {tid: m.now - 5.0}
        m._update_blocks()
        self.assertEqual(m.block_src.get(node_res("NE22_8")), "turn")

    def test_turning_back_on_a_node_heads_for_the_node_it_came_from(self):
        from fleet.agents import Forklift
        layout = warehouse_layout()
        loop = ["A1", "A2", "A3", "A4"]
        f = Forklift("F", loop, layout, random.Random(1), idx=1)
        n = layout.nodes["A2"]
        f.bx, f.by = n.x, n.y
        f._reverse()
        self.assertEqual(f.loop[f._target()], "A1")


class ReleaseTests(unittest.TestCase):
    def test_tote_trips_are_released_up_to_the_cap(self):
        sim = quiet_dc(amr=4)
        d = sim.dispatcher
        cap = sim.manager.cfg.pick_aisle_capacity * len(sim.manager.pick_aisles)
        self.assertEqual(d._tote_room(), cap)
        sim.manager.cfg.pick_aisle_capacity = 0
        self.assertEqual(d._tote_room(), math.inf)

    def test_a_fleet_larger_than_its_waiting_places_is_reported(self):
        from fleet.scenario import get
        from fleet.sim import Simulation
        self.assertEqual(Simulation(get("standard"), seed=1).capacity_warnings, [])
        self.assertTrue(Simulation(get("standard", amr=22), seed=1).capacity_warnings)


class FireAndFleetStopTests(unittest.TestCase):
    def test_a_fire_alarm_keeps_a_fleet_stop_and_its_release_returns_to_fire_mode(self):
        sim = quiet_dc()
        ops = Operations(sim)
        self.assertTrue(ops.execute({"cmd": "fleet_estop", "reason": "훈련", "confirm": True})["ok"])
        sim.run(2)
        sim.drill_fire(True)
        sim.run(3)
        m = sim.manager
        self.assertEqual((m.mode, m.fire_alarm), ("ESTOP", True))
        self.assertTrue(sim.vehicles[0].estop)                           # the remote stop was never released
        self.assertTrue(ops.execute({"cmd": "fleet_estop_release", "confirm": True})["ok"])
        self.assertEqual(m.mode, "FIRE")
        self.assertFalse(ops.execute({"cmd": "fleet_resume", "confirm": True})["ok"])
        sim.drill_fire(False)
        sim.run(1)
        self.assertEqual(m.mode, "HOLD")
        self.assertTrue(ops.execute({"cmd": "fleet_resume", "confirm": True})["ok"])


class SafetyFaultRadioTests(unittest.TestCase):
    def test_the_field_service_message_does_not_clear_a_safety_fault(self):
        layout = warehouse_layout()
        v = Vehicle("AMR-01", layout, "A1", random.Random(1))
        v.set_fault("SENSOR_FAULT")
        v.on_instant_actions({"actions": [{"actionType": "x_fieldServiceDone", "actionId": "f"}]}, 0.0)
        self.assertEqual(v.fault, "SENSOR_FAULT")
        v.local_reset()
        self.assertIsNone(v.fault)


class MeasurementWindowTests(unittest.TestCase):
    def test_warm_up_end_keeps_the_outbound_backlog(self):
        sim = quiet_dc()
        sim.process.out_backlog = 5
        sim.reset_measurement()
        self.assertEqual(sim.process.stats(sim.now)["out_backlog"], 5)

    def test_a_deadlock_from_the_warm_up_is_not_counted_when_it_resolves(self):
        sim = quiet_dc()
        m = sim.manager
        m.deadlocks[frozenset({"X", "Y"})] = {"start": m.now, "members": ["X", "Y"], "attempts": 1,
                                              "next": m.now, "method": "replan"}
        sim.reset_measurement()
        m._deadlocks()
        self.assertEqual((m.counters["deadlocks_resolved"], m.counters["deadlock_s"]), (0, 0.0))

    def test_static_contacts_are_not_counted_twice(self):
        sim = quiet_dc()
        sim.evaluator.contacts = [{"moving": False, "other_moving": True, "kind": "human"},
                                  {"moving": False, "other_moving": False, "kind": "amr"},
                                  {"moving": True, "other_moving": False, "kind": "human"}]
        k = sim.kpi()
        self.assertEqual((k["contacts"], k["contacts_moving"], k["contacts_struck"], k["contacts_static"]),
                         (3, 1, 1, 1))


class NearMissTests(unittest.TestCase):
    def test_only_a_person_in_the_path_is_inside_the_stopping_envelope(self):
        from fleet.agents import Human
        sim = quiet_dc()
        v = sim.vehicles[0]
        v.v, v.theta = 1.0, 0.0
        beside = Human("B1", "walker", v.x, v.y + v.spec.width / 2 + 0.6, random.Random(1))
        sim.evaluator.check(sim.now, [v], [beside], [], [])
        self.assertEqual(sim.evaluator.near_misses, 0)
        # ahead but clear of the swept width (a picker at the shelf face as the vehicle passes): not in the path
        aside = Human("S1", "walker", v.x + v.spec.length / 2 + 0.6, v.y + v.spec.width / 2 + 0.4, random.Random(3))
        sim.evaluator.check(sim.now + 5, [v], [aside], [], [])
        self.assertEqual(sim.evaluator.near_misses, 0)
        ahead = Human("A1", "walker", v.x + v.spec.length / 2 + 0.6, v.y, random.Random(2))
        sim.evaluator.check(sim.now + 11, [v], [ahead], [], [])
        self.assertEqual(sim.evaluator.near_misses, 1)

    def test_the_control_room_needs_two_separate_observations(self):
        sim = quiet_dc()
        m = sim.manager
        rec = m.robots["AGF-01"]
        rec.v, rec.theta = 1.0, 0.0
        tid = confirmed_track(m, "human", rec.x + rec.length / 2 + 0.4, rec.y)
        before = m.counters["near_miss"]
        m._near_misses()
        m._near_misses()                                       # the same camera frame seen twice
        self.assertEqual(m.counters["near_miss"], before)
        m.tracker.tracks[tid].last_seen += 0.5                 # a new observation of the same person
        m._near_misses()
        self.assertEqual(m.counters["near_miss"], before + 1)

    def test_the_factsheet_states_the_braking_deceleration(self):
        v = Vehicle("AGF-01", warehouse_layout(), "A1", random.Random(1))
        self.assertEqual(v.factsheet_msg(0.0)["physicalParameters"]["decelerationMax"], v.spec.stop_decel)


class TrackMergeTests(unittest.TestCase):
    def test_two_people_side_by_side_stay_two_tracks(self):
        tr = Tracker()
        for t in (0.0, 0.5, 1.0):
            tr.update([Detection("human", 10.0, 10.0, "infra"), Detection("human", 10.75, 10.0, "infra")], t)
        self.assertEqual(len(tr.tracks), 2)

    def test_a_misread_class_on_one_body_is_merged(self):
        tr = Tracker()
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.0)
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.5)
        tr.tracks[next(iter(tr.tracks))].first_seen = -1.0
        from fleet.perception import Track
        tr.tracks["ghost"] = Track("ghost", "human", 10.5, 10.0, last_seen=0.5, first_seen=0.5,
                                   votes={"human": 1.0})
        tr._merge_duplicates()
        self.assertEqual(len(tr.tracks), 1)


class RestartTests(unittest.TestCase):
    def test_the_newest_snapshot_wins_a_contested_reservation(self):
        layout = warehouse_layout()
        m = FleetManager(layout, Bus(random.Random(1)))
        res = node_res("B2")
        m.restore({"AMR-01": {"held": [res], "saved_at": 10.0, "last_node": "B2"},
                   "AMR-02": {"held": [res], "saved_at": 12.0, "last_node": "B3"}})
        self.assertEqual(m.locks.holder_of(res), "AMR-02")

    def test_the_snapshot_is_saved_before_the_order_goes_out(self):
        from tests.helpers import MiniWorld
        w = MiniWorld({"AMR-01": "A1"})
        w.run(1.0)
        seen = []
        w.manager.on_order = lambda rec: seen.append((rec.last_order["orderId"], len(w.bus._queue)))
        queued = len(w.bus._queue)
        w.send("AMR-01", "A5")
        w.manager.tick(w.now, 0.2)
        self.assertTrue(seen)
        self.assertEqual(seen[0][1], queued)                    # nothing published yet when the hook ran

    def test_an_unconfirmed_cancel_is_sent_again(self):
        from tests.helpers import MiniWorld
        w = MiniWorld({"AMR-01": "A1"})
        w.run(1.0)
        m = w.manager
        rec = m.robots["AMR-01"]
        rec.state_order_id, rec.state_remaining = "OLD", 3
        m.cancel_pending["AMR-01"] = ("OLD", m.now - 5.0)
        before = len(w.bus._queue)
        m._confirm_cancels()
        self.assertGreater(len(w.bus._queue), before)
        rec.state_remaining = 0
        m._confirm_cancels()
        self.assertNotIn("AMR-01", m.cancel_pending)


class RuntimeFaultTests(unittest.TestCase):
    def runtime(self, store=None) -> Runtime:
        return Runtime(warehouse_layout(), "127.0.0.1", 1, store or Store(":memory:"), run_id="t1")

    def test_an_internal_fault_never_weakens_a_fleet_stop(self):
        rt = self.runtime()
        rt.manager.mode = "ESTOP"
        rt._fail("시험", ValueError("boom"))
        self.assertEqual(rt.manager.mode, "ESTOP")
        rt.manager.mode = "RUNNING"
        rt._fail("시험", ValueError("boom"))
        self.assertEqual(rt.manager.mode, "HOLD")
        self.assertTrue(rt.manager.alarms.is_active("FMS_INTERNAL_ERROR:fms"))

    def test_a_storage_failure_holds_the_fleet_and_the_loop_goes_on(self):
        rt = self.runtime()
        rt.manager.register("AMR-01")

        def broken(_snaps):
            raise OSError("disk full")
        rt.store.save_robots = broken
        rt._next_save = 0.0
        rt.step()
        self.assertEqual(rt.manager.mode, "HOLD")
        rt.step()                                              # still runs

    def test_the_clock_continues_across_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fms.sqlite3")
            first = self.runtime(store)
            t1 = first.clock.now()
            time.sleep(0.3)
            second = self.runtime(store)
            self.assertGreater(second.clock.now(), t1 + 0.25)                  # same time base, not 0 again
            self.assertGreater(second.manager.ids.start, first.manager.ids.start)
            store.close()

    def test_a_retained_door_state_is_not_a_live_answer(self):
        bus = Bus(random.Random(1))
        sup = DoorSupervisor({"D1": DoorSpec("D1", "문", (0, 0), (0, 2))}, bus, None, lambda *a, **k: None)
        sup._on_state("facility/v1/door/D1/state", {"currentMode": "OPEN", "_retained": True})
        self.assertNotEqual(sup.view["D1"].mode, "OPEN")
        sup._on_state("facility/v1/door/D1/state", {"currentMode": None})          # malformed
        self.assertNotEqual(sup.view["D1"].mode, None)

    def test_the_managers_own_state_is_retained(self):
        self.assertEqual(qos_retain("fleet/v1/fms/state"), (1, True))


class AuthHardeningTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.auth = Auth(self.store)
        self.auth.create_user("op", "operator-pass-1", "operator")

    def test_a_lockout_is_per_address_and_unknown_accounts_answer_alike(self):
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.auth.login("op", "wrong-password", "10.0.0.9")
            with self.assertRaises(AuthError):
                self.auth.login("ghost", "wrong-password", "10.0.0.9")
        for name in ("op", "ghost"):
            with self.assertRaises(AuthError) as ctx:
                self.auth.login(name, "operator-pass-1", "10.0.0.9")
            self.assertEqual(ctx.exception.status, 429)
        self.auth.login("op", "operator-pass-1", "10.0.0.20")        # the operator's own console still works

    def test_a_new_password_ends_the_old_sessions(self):
        token, _ = self.auth.login("op", "operator-pass-1", "10.0.0.20")
        self.assertIsNotNone(self.auth.session(token))
        self.auth.create_user("op", "operator-pass-2", "operator")
        self.assertIsNone(self.auth.session(token))

    def test_the_audit_log_notices_a_cut_off_end_and_a_recomputed_chain(self):
        for i in range(3):
            self.store.append_audit({"cmd": f"c{i}"})
        self.assertEqual(self.store.verify_audit(), [])
        with self.store.lock:
            self.store.db.execute("DELETE FROM audit WHERE seq = (SELECT MAX(seq) FROM audit)")
        self.assertTrue(self.store.verify_audit())
        # rebuild the whole chain with plain SHA-256 (what a forger without the key can do)
        with self.store.lock:
            rows = self.store.db.execute("SELECT seq, body FROM audit ORDER BY seq").fetchall()
            prev = "0" * 64
            for seq, body in rows:
                digest = chain_hash(prev, body)
                self.store.db.execute("UPDATE audit SET prev=?, hash=? WHERE seq=?", (prev, digest, seq))
                prev = digest
        self.assertTrue(self.store.verify_audit())

    def test_the_acknowledging_account_is_the_session_not_a_claimed_name(self):
        sim = quiet_dc()
        ops = Operations(sim)
        sim.manager.alarms.set("CONGESTION", "fleet", True, sim.now, "시험")
        key = next(iter(sim.manager.alarms.alarms))
        ops.execute({"cmd": "ack_alarm", "key": key, "by": "mallory"}, operator="kim")
        self.assertEqual(sim.manager.alarms.alarms[key].acked_by, "kim")


class WmsIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.sim = quiet_dc()
        self.ops = Operations(self.sim)
        self.order = {"order_id": "W-1", "kind": "PALLET_OUT", "source": "R1-03"}

    def test_a_key_whose_task_never_existed_takes_the_order_again(self):
        seen = {"W-1": {"body": {"kind": "PALLET_OUT", "source": "R1-03", "dest": None, "priority": 0},
                        "task": "T99999"}}
        res = self.ops.submit_wms_order(self.order, seen)
        self.assertFalse(res["duplicate"])
        self.assertIn(res["task"], self.sim.dispatcher.tasks)

    def test_a_task_without_its_key_is_not_created_twice(self):
        first = self.ops.submit_wms_order(self.order, {})          # key lost (another dict)
        again = self.ops.submit_wms_order(self.order, {})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["task"], first["task"])


class HttpHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.auth = Auth(cls.store)
        cls.auth.create_user("op", "operator-pass-1", "operator")
        cls.runner = Runner("grid-night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=False,
                            store=cls.store)
        cls.httpd, cls.base = serve(cls.runner, cls.auth)
        cls.dev, cls.dev_base = serve(Runner("grid-night", 3, "locks+resolve", 1.0, auto_operator=False,
                                             read_only=False), None)

    @classmethod
    def tearDownClass(cls):
        for h in (cls.httpd, cls.dev):
            h.shutdown()
            h.server_close()

    def raw(self, base: str, method: str, path: str, body: bytes = b"", headers=None):
        host, port = base.split("//")[1].split(":")
        conn = HTTPConnection(host, int(port), timeout=5)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in {"Host": f"{host}:{port}", **(headers or {})}.items():
            conn.putheader(k, v)
        conn.endheaders(body or None)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_a_negative_body_length_is_refused(self):
        status, _ = self.raw(self.base, "POST", "/api/login", b"{}",
                             {"Content-Type": "application/json", "Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_login_from_another_site_is_refused(self):
        body = b'{"name":"op","password":"operator-pass-1"}'
        status, _ = self.raw(self.base, "POST", "/api/login", body,
                             {"Content-Type": "application/json", "Origin": "https://evil.example",
                              "Content-Length": str(len(body))})
        self.assertEqual(status, 403)

    def test_malformed_query_values_are_a_bad_request(self):
        c = Client(self.base)
        c.login("op", "operator-pass-1")
        self.assertEqual(c.call("GET", "/api/vda5050/AMR-01?limit=lots")[0], 400)

    def test_development_mode_answers_only_this_machine(self):
        status, _ = self.raw(self.dev_base, "GET", "/api/snapshot", headers={"Host": "evil.example:8095"})
        self.assertEqual(status, 403)
        body = json.dumps({"cmd": "fleet_hold", "confirm": True}).encode()
        status, _ = self.raw(self.dev_base, "POST", "/api/command", body,
                             {"Content-Type": "text/plain", "Content-Length": str(len(body))})
        self.assertEqual(status, 415)


if __name__ == "__main__":
    unittest.main()
