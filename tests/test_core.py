"""Unit tests for the control core: messages, vehicle rules, reservations, alarms, tracking."""
from __future__ import annotations

import doctest
import math
import random
import unittest

from fleet import bus as bus_mod
from fleet import vehicle as vehicle_mod
from fleet import control, geometry, traffic
from fleet.alarms import AlarmManager
from fleet.bus import Bus
from fleet.control import RobotRecord, near_miss
from fleet.layout import warehouse_layout
from fleet.perception import Detection, Tracker
from fleet.traffic import LockTable, find_cycles
from fleet.vda5050 import (HeaderIds, RouteEdge, RouteNode, build_order, header, validate_order)
from fleet.vehicle import Obstacle, Vehicle

from tests.helpers import MiniWorld


def load_tests(loader, tests, ignore):
    for mod in (geometry, traffic, bus_mod, vehicle_mod):
        tests.addTests(doctest.DocTestSuite(mod))
    return tests


def order_for(layout, route, order_id="O1", update=0, start=0, released=None):
    ids = HeaderIds()
    nodes = [RouteNode(n, layout.nodes[n].x, layout.nodes[n].y) for n in route[start:]]
    edges = [RouteEdge(layout.edge_between(a, b).id, a, b, 1.5) for a, b in zip(route[start:], route[start + 1:])]
    return build_order(header(ids, "t", "sim", "AMR-01", 0.0), order_id, update, nodes, edges, 2 * start,
                       released or len(nodes))


class LayoutTests(unittest.TestCase):
    def test_every_node_reaches_every_node(self):
        for two_way in (False, True):
            self.assertEqual(warehouse_layout(two_way).validate(), [])

    def test_one_way_design(self):
        L = warehouse_layout()
        self.assertIn("B2", {e.end for e in L.out_edges["B1"]})       # B runs north
        self.assertNotIn("B1", {e.end for e in L.out_edges["B2"]})
        q = "qA9B9"                                                   # row 9 runs west
        self.assertIn(q, {e.end for e in L.out_edges["B9"]})
        self.assertNotIn(q, {e.end for e in L.out_edges["A9"]})

    def test_dc_classes_are_strongly_connected(self):
        from fleet.site_dc import AGF, AMR, dc_layout
        L = dc_layout()
        self.assertEqual(L.validate_class(AGF), [])
        self.assertEqual(L.validate_class(AMR), [])
        self.assertFalse(any(L.is_intersection(n) for n in L.nodes_of_kind("queue")))

    def test_holding_nodes_are_not_intersections(self):
        L = warehouse_layout()
        queues = L.nodes_of_kind("queue")
        self.assertEqual(len(queues), 43)
        self.assertFalse(any(L.is_intersection(q) for q in queues))


class Vda5050Tests(unittest.TestCase):
    def setUp(self):
        self.L = warehouse_layout()

    def test_sequence_ids_and_base_prefix(self):
        msg = order_for(self.L, ["B1", "B2", "B3"], released=2)
        self.assertEqual([n["sequenceId"] for n in msg["nodes"]], [0, 2, 4])
        self.assertEqual([e["sequenceId"] for e in msg["edges"]], [1, 3])
        self.assertEqual([n["released"] for n in msg["nodes"]], [True, True, False])
        self.assertEqual([e["released"] for e in msg["edges"]], [True, False])
        self.assertEqual(validate_order(msg), [])

    def test_validator_rejects_horizon_before_base(self):
        msg = order_for(self.L, ["B1", "B2", "B3"])
        msg["nodes"][1]["released"] = False
        self.assertTrue(any("prefix" in p for p in validate_order(msg)))

    def test_bus_latency_and_outage(self):
        b = Bus(random.Random(0))
        got = []
        b.subscribe("uagv/v2/+/+/state", lambda t, m: got.append(m))
        b.link("AMR-01").jitter_s = 0.0
        b.publish("uagv/v2/sim/AMR-01/state", {"a": 1}, 0.0)
        b.deliver_due(0.01)
        self.assertEqual(got, [])
        b.deliver_due(0.06)
        self.assertEqual(len(got), 1)
        b.link("AMR-01").down_until = 5.0
        self.assertFalse(b.publish("uagv/v2/sim/AMR-01/state", {"a": 2}, 1.0))
        self.assertEqual(b.stats["dropped"], 1)


class VehicleTests(unittest.TestCase):
    def setUp(self):
        self.L = warehouse_layout()
        self.v = Vehicle("AMR-01", self.L, "B1", random.Random(1))
        self.v.perf = 1.0
        self.t = 0.0

    def drive(self, seconds, obstacles=()):
        for _ in range(int(seconds / 0.1)):
            self.t += 0.1
            self.v.step(0.1, self.t, list(obstacles))

    def test_stops_at_base_end(self):
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3", "B4"], released=2))
        self.drive(20)
        self.assertEqual(self.v.last_node_id, "B2")
        self.assertEqual(self.v.v, 0.0)
        self.assertEqual(self.v.stop_reason, "경로 해제 대기")

    def test_update_must_start_at_base_end(self):
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3", "B4"], released=2))
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3", "B4"], update=1, start=0, released=3))
        self.assertEqual(self.v.errors[-1]["errorType"], "orderUpdateError")
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3", "B4"], update=1, start=1, released=2))
        self.assertEqual([n["released"] for n in self.v.nodes], [True, True, True, False])
        self.drive(30)
        self.assertEqual(self.v.last_node_id, "B3")

    def test_new_order_rejected_while_base_unfinished(self):
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3"], released=3))
        self.drive(1.5)
        self.v.on_order(order_for(self.L, ["B1", "B2"], order_id="O2"))
        self.assertEqual(self.v.errors[-1]["errorType"], "orderError")
        self.assertEqual(self.v.order_id, "O1")

    def test_protective_stop_and_auto_resume(self):
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3"]))
        person = Obstacle("H1", "human", 9.0, 8.0, 0.3)
        for _ in range(300):
            self.drive(0.1, [person])
            if self.v.field == "PROTECTIVE" and self.v.v == 0.0:
                break
        self.assertEqual(self.v.field, "PROTECTIVE")
        self.assertEqual(self.v.v, 0.0)
        self.assertLess(self.v.y, 8.0 - 0.3 - 0.45)                 # never touched the person
        self.assertGreaterEqual(self.v.protective_stops, 1)
        self.drive(1.0)
        self.assertEqual(self.v.v, 0.0)                               # still inside the 2 s clear time
        self.drive(20)
        self.assertEqual(self.v.last_node_id, "B3")

    def test_pause_and_fault_latch(self):
        self.v.on_order(order_for(self.L, ["B1", "B2", "B3"]))
        self.v.on_instant_actions({"actions": [{"actionType": "startPause", "actionId": "p"}]}, 0.0)
        self.drive(5)
        self.assertEqual(self.v.last_node_id, "B1")
        self.v.on_instant_actions({"actions": [{"actionType": "stopPause", "actionId": "r"}]}, 0.0)
        self.v.set_fault("DRIVE_FAULT", recoverable=False)
        self.drive(5)
        self.assertLess(self.v.y, 4.0)
        self.v.on_instant_actions({"actions": [{"actionType": "x_resetFault", "actionId": "x"}]}, 0.0)
        self.assertEqual(self.v.fault, "DRIVE_FAULT")                 # unrecoverable: remote reset refused
        self.v.on_instant_actions({"actions": [{"actionType": "x_fieldServiceDone", "actionId": "y"}]}, 0.0)
        self.assertIsNone(self.v.fault)

    def test_manual_mode_rejects_orders_and_has_deadman(self):
        self.v.on_instant_actions({"actions": [{"actionType": "x_setOperatingMode", "actionId": "m",
                                                "actionParameters": [{"key": "mode", "value": "MANUAL"}]}]}, 0.0)
        self.v.on_order(order_for(self.L, ["B1", "B2"], order_id="O9"))
        self.assertEqual(self.v.errors[-1]["errorType"], "orderError")
        self.v.theta = math.pi / 2
        self.v.on_instant_actions({"actions": [{"actionType": "x_teleop", "actionId": "t",
                                                "actionParameters": [{"key": "vx", "value": 0.4}]}]}, self.t)
        y0 = self.v.y
        self.drive(2.0)
        moved = self.v.y - y0
        self.assertGreater(moved, 0.1)
        self.assertLess(moved, 0.4 * 0.7)                             # stops after the 0.6 s deadman

    def test_charging(self):
        v = Vehicle("AMR-02", self.L, "C1", random.Random(2), battery=40.0)
        route = ["C1", "CH1"]
        msg = order_for(self.L, route)
        msg["nodes"][-1]["actions"] = [{"actionType": "startCharging", "actionId": "c1", "blockingType": "HARD",
                                        "actionParameters": []}]
        v.on_order(msg)
        t = 0.0
        for _ in range(200):
            t += 0.1
            v.step(0.1, t, [])
        self.assertTrue(v.charging)
        self.assertGreater(v.battery, 40.0)


class LockAndCycleTests(unittest.TestCase):
    def test_all_or_nothing(self):
        lt = LockTable()
        self.assertTrue(lt.try_acquire("A", ["N:1", "N:2"])[0])
        ok, holder, res = lt.try_acquire("B", ["N:3", "N:2"])
        self.assertFalse(ok)
        self.assertEqual((holder, res), ("A", "N:2"))
        self.assertIsNone(lt.holder_of("N:3"))                        # nothing partial
        lt.retain_only("A", {"N:2"})
        self.assertEqual(lt.resources_of("A"), ["N:2"])
        self.assertEqual(lt.check(), [])

    def test_cycles(self):
        self.assertEqual(find_cycles({"A": "B", "B": "C", "C": "A", "D": "A"}), [["A", "B", "C"]])


class TrafficTests(unittest.TestCase):
    def test_head_on_deadlock_detected_and_resolved(self):
        w = MiniWorld({"AMR-01": "A1", "AMR-02": "C1"}, two_way=True)
        w.run(1)
        w.send("AMR-01", "C1")
        w.send("AMR-02", "A1")
        ok = w.run_until(lambda: w.at("AMR-01", "C1") and w.at("AMR-02", "A1"), 300)
        self.assertTrue(ok, "both robots reach their goals")
        self.assertGreaterEqual(w.manager.counters["deadlocks"], 1)
        self.assertGreaterEqual(w.manager.counters["deadlocks_resolved"], 1)
        self.assertEqual(w.manager.locks.check(), [])

    def test_locks_only_detects_but_stays_stuck(self):
        w = MiniWorld({"AMR-01": "A1", "AMR-02": "C1"}, two_way=True, strategy="locks")
        w.run(1)
        w.send("AMR-01", "C1")
        w.send("AMR-02", "A1")
        w.run(120)
        self.assertGreaterEqual(w.manager.counters["deadlocks"], 1)
        self.assertEqual(w.manager.counters["deadlocks_resolved"], 0)
        self.assertFalse(w.at("AMR-01", "C1"))

    def test_nodes_never_shared(self):
        w = MiniWorld({f"AMR-{i:02d}": n for i, n in enumerate(["A1", "B1", "C1", "D1", "E1", "F1"], 1)})
        w.run(1)
        goals = ["J9", "A9", "E9", "C9", "G9", "I9"]
        for i, g in enumerate(goals, 1):
            w.send(f"AMR-{i:02d}", g)
        for _ in range(1500):
            w.step()
            self.assertEqual(w.manager.locks.check(), [])
            pts = [(v.x, v.y) for v in w.vehicles.values()]
            for i, a in enumerate(pts):
                for b in pts[i + 1:]:
                    self.assertGreater(math.hypot(a[0] - b[0], a[1] - b[1]), 0.9)

    def test_person_prediction_blocks_then_releases(self):
        w = MiniWorld({"AMR-01": "B1"})
        w.run(1)
        w.detections = [Detection("human", 9.0, 10.0, "infra")]     # standing on B3
        w.run(1)
        w.send("AMR-01", "B4")
        w.run(20)
        rec = w.manager.robots["AMR-01"]
        self.assertEqual(rec.last_node, "B2")
        self.assertIsNotNone(rec.wait)
        self.assertEqual(rec.wait.kind, "human")
        w.detections = []                                             # person left; the feed keeps running
        self.assertTrue(w.run_until(lambda: w.at("AMR-01", "B4"), 40))

    def test_infra_feed_loss_raises_alarm_and_slows_zones(self):
        w = MiniWorld({"AMR-01": "B1"})
        w.detections = [Detection("human", 1.4, 8.25, "infra")]
        w.run(2)
        w.feed_active = False
        w.detections = []
        w.run(5)
        self.assertTrue(w.manager.alarms.alarms["INFRA_FEED_LOST:infra"].active)
        self.assertEqual(w.manager.tracker.tracks, {})                # stale tracks expired, not grown
        self.assertIn("Z-PICK-D", w.manager.active_zone_limits())

    def test_zone_lock_blocks_entry(self):
        w = MiniWorld({"AMR-01": "B1"})
        w.layout.zones["Z-B-S"].locked = True
        w.run(1)
        w.send("AMR-01", "B4")
        w.run(15)
        self.assertEqual(w.manager.robots["AMR-01"].wait.kind, "zone")
        w.layout.zones["Z-B-S"].locked = False
        self.assertTrue(w.run_until(lambda: w.at("AMR-01", "B4"), 40))

    def test_comms_loss_keeps_reservations_and_vehicle_stops_at_base_end(self):
        w = MiniWorld({"AMR-01": "B1"})
        w.run(1)
        w.send("AMR-01", "B9")
        w.run(3)
        w.bus.link("AMR-01").down_until = w.now + 25.0
        held_before = set(w.manager.locks.resources_of("AMR-01"))
        w.run(20)
        rec = w.manager.robots["AMR-01"]
        self.assertEqual(rec.connection, "CONNECTIONBROKEN")
        self.assertTrue(held_before <= set(w.manager.locks.resources_of("AMR-01")))
        v = w.vehicles["AMR-01"]
        base_end = v.nodes[v.base_end_index()]["nodeId"]
        self.assertEqual(v.last_node_id, base_end)                    # drove only what was released
        self.assertTrue(w.run_until(lambda: w.at("AMR-01", "B9"), 60))
        self.assertEqual(rec.connection, "ONLINE")

    def test_external_itinerary_is_respected(self):
        w = MiniWorld({"AMR-01": "B2"})
        w.external = ("EXT-1", 9.0, 17.0, 0.0, ["B5", "qB5C5", "C5", "qC5D5"])   # stationary on B5
        w.run(1)
        w.send("AMR-01", "B6")
        rec = w.manager.robots["AMR-01"]
        self.assertTrue(w.run_until(lambda: rec.wait is not None, 30))
        self.assertEqual(rec.wait.kind, "external")
        self.assertEqual(w.manager.locks.holder_of("N:B5"), "EXT-1")

    def test_occupancy_monitor_counts_node_area_not_adjacent_lanes(self):
        w = MiniWorld({"AMR-01": "A1"})
        rec = RobotRecord("X")
        rec.x, rec.y = 8.5, 3.0                                        # 0.5 m before intersection B1
        occ = w.manager._occupied(rec)
        self.assertEqual(occ, {"N:B1"})
        rec.x = 7.5                                                    # mid-lane between qA1B1 and B1
        self.assertTrue(all(r.startswith("L:") for r in w.manager._occupied(rec)))


class AlarmTests(unittest.TestCase):
    def test_lifecycle(self):
        am = AlarmManager()
        am.set("ROBOT_FAULT", "AMR-01", True, 0.0, "x")
        am.ack("ROBOT_FAULT:AMR-01", "op", 5.0)
        am.set("ROBOT_FAULT", "AMR-01", False, 10.0)
        self.assertNotIn("ROBOT_FAULT:AMR-01", am.alarms)             # acked + cleared -> closed
        am.set("ROBOT_FAULT", "AMR-02", True, 0.0)
        am.set("ROBOT_FAULT", "AMR-02", False, 1.0)
        self.assertEqual(am.alarms["ROBOT_FAULT:AMR-02"].state, "RTN_UNACK")
        am.ack("ROBOT_FAULT:AMR-02", "op", 2.0)
        self.assertNotIn("ROBOT_FAULT:AMR-02", am.alarms)

    def test_shelving_rules(self):
        am = AlarmManager()
        am.set("HUMAN_CONTACT", "AMR-01", True, 0.0)
        with self.assertRaises(ValueError):
            am.shelve("HUMAN_CONTACT:AMR-01", "op", 0.0, 60, "test")
        am.set("NEAR_MISS", "AMR-01", True, 0.0)
        with self.assertRaises(ValueError):
            am.shelve("NEAR_MISS:AMR-01", "op", 0.0, 60, " ")
        am.shelve("NEAR_MISS:AMR-01", "op", 0.0, 60, "현장 확인 중")
        am.tick(30.0)
        self.assertEqual(am.alarms["NEAR_MISS:AMR-01"].state, "SHELVED")
        am.tick(61.0)
        self.assertEqual(am.alarms["NEAR_MISS:AMR-01"].state, "UNACK")

    def test_flood_and_off_delay(self):
        am = AlarmManager()
        for i in range(11):
            am.set("ROBOT_FAULT", f"AMR-{i}", True, float(i))
        am.tick(12.0)
        self.assertTrue(am.alarms["ALARM_FLOOD:fleet"].active)
        am.set("BLOCKED_LONG", "AMR-01", True, 0.0)
        am.set("BLOCKED_LONG", "AMR-01", False, 1.0)
        self.assertTrue(am.alarms["BLOCKED_LONG:AMR-01"].active)      # chatter suppressed
        am.set("BLOCKED_LONG", "AMR-01", False, 40.0)
        self.assertFalse(am.alarms["BLOCKED_LONG:AMR-01"].active)


class PerceptionTests(unittest.TestCase):
    def test_association_and_loss(self):
        tr = Tracker()
        for k in range(6):
            tr.update([Detection("human", 5.0 + 0.5 * k, 3.0, "infra"),
                       Detection("human", 5.05 + 0.5 * k, 3.02, "amr:AMR-01")], k * 0.5)
        self.assertEqual(len(tr.tracks), 1)
        track = next(iter(tr.tracks.values()))
        self.assertGreater(track.vx, 0.5)
        self.assertEqual(tr.update([], 5.0), [])                     # last seen 2.5 s: kept
        self.assertEqual([e["type"] for e in tr.update([], 6.0)], ["track_lost"])

    def test_standing_person_does_not_sweep(self):
        tr = Tracker()
        rng = random.Random(3)
        for k in range(20):
            tr.update([Detection("human", 5.0 + rng.gauss(0, 0.12), 3.0 + rng.gauss(0, 0.12), "infra")], k * 0.5)
        discs = tr.predicted_discs(10.0, {"human": 2.0})
        self.assertEqual(len(discs), 1)                               # no forward prediction when still


class SafetyRuleTests(unittest.TestCase):
    def test_near_miss_rule(self):
        self.assertFalse(near_miss(0.3, 0.15))       # reduced clearance at <= 0.3 m/s is allowed
        self.assertTrue(near_miss(0.3, 0.05))        # but under 0.1 m is always a near miss
        self.assertTrue(near_miss(1.0, 0.25))
        self.assertFalse(near_miss(1.0, 0.35))


if __name__ == "__main__":
    unittest.main()
