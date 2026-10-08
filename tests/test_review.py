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


def drive_track(manager, legs, dt: float = 0.5, sigma: float = 0.0, seed: int = 0, look=None):
    """Drive one truck's detections through the control room: from ``legs[0]`` (a point) along
    ``(point, speed m/s)`` legs; ``("wait", seconds)`` stands still. Each detection (one per ``dt``, Gaussian
    noise ``sigma`` as from the cameras) goes to the tracker and the block update, on the manager's clock.
    ``look(track)`` is called after each update. Returns the track."""
    rng = random.Random(seed)
    points = [legs[0]]
    for target, v in legs[1:]:
        px, py = points[-1]
        if target == "wait":
            points += [(px, py)] * int(v / dt)
            continue
        n = max(1, round(math.hypot(target[0] - px, target[1] - py) / (v * dt)))
        points += [(px + (target[0] - px) * k / n, py + (target[1] - py) * k / n) for k in range(1, n + 1)]
    manager.tracker = Tracker()
    manager._fl_stood = {}
    manager._fl_still_since = {}
    for x, y in points:
        manager.now += dt
        manager.tracker.update([Detection("forklift", x + rng.gauss(0, sigma), y + rng.gauss(0, sigma), "infra")],
                               manager.now)
        manager._update_blocks()
        if look is not None and manager.tracker.tracks:
            look(next(iter(manager.tracker.tracks.values())))
    (track,) = manager.tracker.tracks.values()
    return track


class StoppedTruckTests(unittest.TestCase):
    # FL-R1's two corners: west along the south main aisle into R1, east along the north one into R2
    CORNERS = (("SW16_2", (1, 0), "Z-R1"), ("NE22_8", (-1, 0), "Z-R2"))

    def setUp(self):
        self.sim = quiet_dc()
        self.m = self.sim.manager
        self.L = self.sim.layout

    def at(self, node: str, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
        n = self.L.nodes[node]
        return (n.x + dx, n.y + dy)

    def backed_off(self, node, back, metres, sigma=0.0, seed=0, then=(), look=None):
        bx, by = back
        return drive_track(self.m, [self.at(node, 9 * bx, 9 * by), (self.at(node), 2.0), ("wait", 12),
                                    (self.at(node, metres * bx, metres * by), 0.5), *then, ("wait", 20)],
                           sigma=sigma, seed=seed, look=look)

    def test_a_truck_that_backed_off_waits_for_the_intersection_it_backed_away_from(self):
        for node, back, aisle in self.CORNERS:
            for metres in (3.0, 4.5, 6.0):
                with self.subTest(node=node, metres=metres):
                    tr = self.backed_off(node, back, metres)
                    self.assertEqual(self.m._turn_ahead(tr), node)
                    self.m._fl_still_since = {tr.id: self.m.now - 5.0}
                    self.m._update_blocks()
                    self.assertEqual(self.m.block_src.get(node_res(node)), "turn")
                    # the aisle it turns into, and no other (it may stand in front of the next aisle's mouth)
                    self.assertEqual({z for z, _t in self.m.aisle_requests.values()}, {aisle})

    def test_the_judgement_holds_with_camera_noise(self):
        # cameras: 0.25 m; while the truck stands, the control room keeps naming the right intersection
        def held(legs, want, seed):
            seen = []
            drive_track(self.m, legs, sigma=0.25, seed=seed,
                        look=lambda tr: seen.append(self.m._turn_ahead(tr)) if tr.resting else None)
            tail = seen[-30:]                                   # the last 15 s standing
            self.assertGreaterEqual(tail.count(want) / len(tail), 0.95, tail)
        for seed in (1, 2, 3):
            for node, (bx, by), _aisle in self.CORNERS:
                for metres in (3.0, 4.5, 6.0):
                    with self.subTest(node=node, metres=metres, seed=seed):
                        held([self.at(node, 9 * bx, 9 * by), (self.at(node), 2.0), ("wait", 12),
                              (self.at(node, metres * bx, metres * by), 0.5), ("wait", 20)], node, seed)
            with self.subTest("stopped on the way, on red", seed=seed):
                held([self.at("SW16_2", 9, 0), (self.at("qSW16_2SW22_8"), 2.0), ("wait", 20)], "SW16_2", seed)
            with self.subTest("turned back at a corner", seed=seed):
                held([self.at("SW16_2", 9, 0), (self.at("SW16_2"), 2.0), ("wait", 12),
                      (self.at("SW16_2", 5.0, 0), 2.0), ("wait", 20)], "SW22_8", seed)

    def test_driving_back_up_part_way_keeps_the_intersection(self):
        bx = 1
        tr = self.backed_off("SW16_2", (bx, 0), 4.5, then=[("wait", 4), (self.at("SW16_2", 2.5 * bx, 0), 0.8)])
        self.assertEqual(self.m._turn_ahead(tr), "SW16_2")

    def test_a_truck_that_turned_back_does_not_wait_for_the_corner_it_left(self):
        # stood at SW16_2, turned round and drove back east 5 m: it now waits for SW22_8 ahead, if anything
        tr = drive_track(self.m, [self.at("SW16_2", 9, 0), (self.at("SW16_2"), 2.0), ("wait", 12),
                                  (self.at("SW16_2", 5.0, 0), 2.0), ("wait", 10)])
        self.assertEqual(self.m._turn_ahead(tr), "SW22_8")

    def test_a_truck_driving_the_loop_backwards_is_judged_the_same_way(self):
        # a driver who turned back drives north out of R2 against its flow, waits at NE22_8, backs off south
        tr = drive_track(self.m, [self.at("R2-09", 0, -6), (self.at("NE22_8"), 2.0), ("wait", 12),
                                  (self.at("NE22_8", 0, -3.0), 0.5), ("wait", 10)])
        self.assertEqual(self.m._turn_ahead(tr), "NE22_8")

    def test_a_truck_that_passed_an_intersection_is_not_waiting_at_it(self):
        tr = drive_track(self.m, [self.at("SW16_2", 6, 0), (self.at("SW16_2"), 2.0), ("wait", 3),
                                  (self.at("R1-01"), 2.0), ("wait", 10)])
        self.assertIsNone(self.m._turn_ahead(tr))

    def test_the_aisle_request_stays_while_the_truck_waits_on_red(self):
        # stopped at the queue node short of SW16_2 by the light, past the 20 s the turn request holds
        tr = drive_track(self.m, [self.at("SW16_2", 9, 0), (self.at("qSW16_2SW22_8"), 2.0), ("wait", 10)])
        self.m._fl_still_since = {tr.id: self.m.now - 33.0}
        self.m._update_blocks()
        self.assertNotEqual(self.m.block_src.get(node_res("SW16_2")), "turn")
        self.assertTrue(all(node_res(n) in self.m.aisle_requests for n in self.m._zone_nodes["Z-R1"]))

    def test_a_standing_truck_keeps_its_swing_circle_through_velocity_noise(self):
        seen = []
        drive_track(self.m, [self.at("qSW16_2SW22_8"), ("wait", 60)], sigma=0.25, seed=4,
                    look=lambda tr: seen.append(tr.resting))
        self.assertTrue(all(seen[10:]))            # at rest from the first window on, never "moving" again

    def test_turning_back_on_a_node_heads_for_the_node_it_came_from(self):
        from fleet.agents import Forklift
        layout = warehouse_layout()
        loop = ["A1", "A2", "A3", "A4"]
        f = Forklift("F", loop, layout, random.Random(1), idx=1)
        n = layout.nodes["A2"]
        f.bx, f.by = n.x, n.y
        f._reverse()
        self.assertEqual(f.loop[f._target()], "A1")

    def rack_truck(self, node: str, heading: float, back: float):
        sim = quiet_dc(rack_forklift=1)
        (f,) = [f for f in sim.forklifts if f.id == "FL-R1"]
        f.idx = f.loop.index(node)
        f.bx, f.by = self.at(node)
        f.heading, f.back = heading, back
        f._place(f.heading)
        return f

    def test_turning_back_after_backing_off_retreats_to_the_last_intersection_and_tries_again(self):
        # FL-R1 reached SW16_2 heading west and backed off 4.5 m east, past the queue node 3.3 m back
        f = self.rack_truck("SW16_2", math.pi, 4.5)
        x0 = f.x
        self.assertTrue(f._reverse())
        self.assertEqual((f.back, f.direction), (0.0, -1))
        self.assertEqual(f.loop[f._target()], "SW22_8")
        xs, dirs = [], []
        for k in range(150):
            f.step(self.sim.dt, 1000.0 + k * self.sim.dt, [], [])
            xs.append(f.x)
            dirs.append(f.direction)
        first_back = next(i for i, x in enumerate(xs) if x < x0 - 0.05) if min(xs) < x0 - 0.05 else len(xs)
        self.assertGreaterEqual(max(xs[:first_back]), self.at("SW22_8")[0] - 1e-6)   # first all the way to SW22_8
        self.assertEqual(dirs[-1], 1)                                               # then its own way again
        self.assertEqual(f.loop[f._target()] if f.loop[f.idx] != "SW16_2" else "SW16_2", "SW16_2")

    def test_a_truck_does_not_turn_back_into_a_narrow_aisle(self):
        # FL-R1 came out of R1 at NE16_2 and waits to turn east: the way it came is the one-way aisle
        f = self.rack_truck("NE16_2", math.pi / 2, 0.0)
        self.assertFalse(f._reverse())
        self.assertEqual((f.direction, f.loop[f._target()]), (1, f.loop[(f.loop.index("NE16_2") + 1) % len(f.loop)]))
        gave, answer = f.radio_yield(100.0)
        self.assertTrue(gave)                               # it backs off further instead (6 m for 40 s)
        self.assertEqual((answer, f.direction), ("후진 양보", 1))

    def test_a_truck_backed_into_an_aisle_does_not_turn_round_in_it(self):
        # FL-R1 left R1 at NE16_2 heading north and backed off 4.5 m: its body stands in the aisle
        f = self.rack_truck("NE16_2", math.pi / 2, 4.5)
        f._reverse()
        self.assertEqual((f.direction, f.back), (1, 4.5))
        gave, _answer = f.radio_yield(100.0)
        self.assertFalse(gave)
        self.assertEqual(f.direction, 1)

    def test_no_turn_on_a_straight_aisle(self):
        from fleet.agents import Forklift
        f = Forklift("F", [f"R1-0{k}" for k in range(1, 10)], self.L, random.Random(1), idx=0)
        f.bx, f.by = self.at("R1-01")
        f.heading = math.pi / 2
        f._place(f.heading)
        headings = set()
        for k in range(600):
            f.step(self.sim.dt, 1000.0 + k * self.sim.dt, [], [])
            headings.add(round(f.heading, 3))
            if f.loop[f.idx] == "R1-09":
                break
        self.assertEqual(f.loop[f.idx], "R1-09")
        self.assertEqual(headings, {round(math.pi / 2, 3)})


class AisleLightTests(unittest.TestCase):
    def test_the_light_is_red_while_a_robot_holds_a_place_in_the_aisle(self):
        sim = quiet_dc()
        m = sim.manager
        self.assertTrue(m.aisle_free("Z-R1"))
        m.locks.force("AGF-01", node_res(m._zone_nodes["Z-R1"][0]))
        self.assertFalse(m.aisle_free("Z-R1"))

    def test_on_red_the_truck_stops_short_of_the_junction(self):
        from fleet.agents import Forklift
        sim = quiet_dc()
        L = sim.layout
        f = Forklift("F", ["qSW16_2SW22_8", "SW16_2", "R1-01", "R1-02"], L, random.Random(1), idx=0)
        n = L.nodes["qSW16_2SW22_8"]
        f.bx, f.by = n.x, n.y
        f.aisle_signal = lambda zone_id: False
        self.assertTrue(f._red_beyond(L.nodes["SW16_2"], L.nodes["R1-01"]))
        f.aisle_signal = lambda zone_id: True
        self.assertFalse(f._red_beyond(L.nodes["SW16_2"], L.nodes["R1-01"]))

    def test_a_radioed_driver_turns_back_outside_an_aisle_but_not_inside(self):
        from fleet.agents import Forklift
        sim = quiet_dc()
        L = sim.layout
        f = Forklift("F", ["SW22_8", "qSW16_2SW22_8", "SW16_2", "R1-01"], L, random.Random(1), idx=1)
        f.bx, f.by = L.nodes["qSW16_2SW22_8"].pos
        self.assertEqual(f.radio_yield(100.0), (True, "되돌아감"))
        self.assertEqual(f.direction, -1)
        g = Forklift("G", ["R1-01", "R1-02", "R1-03"], L, random.Random(2), idx=1)
        g.bx, g.by = L.nodes["R1-02"].pos
        gave, answer = g.radio_yield(100.0)
        self.assertEqual(g.direction, 1)                    # inside the aisle: it cannot give way
        self.assertFalse(gave)
        self.assertIn("통로", answer)
        self.assertLess(g.yield_until, 100.0)

    def test_the_radio_call_goes_to_the_truck_the_robot_is_stuck_at(self):
        sim = quiet_dc(rack_forklift=1, dock_forklifts_in=1)
        fl_r1, dock = (next(f for f in sim.forklifts if f.id == i) for i in ("FL-R1", "FL-IN1"))
        v = sim.vehicle("AGF-01")
        v.x, v.y = fl_r1.x + 2.0, fl_r1.y
        v.field_cause = f"forklift:{dock.id}"               # stuck at the dock truck, beside FL-R1
        gave, answer = sim.radio_yield("AGF-01")
        self.assertTrue(gave)
        self.assertIn(dock.id, answer)
        self.assertEqual((fl_r1.radioed, fl_r1.direction), (0, 1))
        v.x, v.y = fl_r1.x + 20.0, fl_r1.y                  # stuck at FL-R1, named by the field, 20 m off
        v.field_cause = "forklift:FL-R1"
        sim.radio_yield("AGF-01")
        self.assertEqual(fl_r1.radioed, 1)

    def test_the_operator_radios_once_per_standoff_and_again_if_the_driver_could_not(self):
        from types import SimpleNamespace
        from fleet.autoop import AutoOperator
        sim = quiet_dc()
        op = AutoOperator(Operations(sim), random.Random(1))
        answers = iter([False, True, True, True])            # first call: the driver is still in an aisle
        calls = []
        op._do = lambda cmd, **a: calls.append(cmd) or {"ok": next(answers)}
        sim.manager.robots["AGF-01"].field_cause = "forklift:FL-R1"
        alarm = SimpleNamespace(type="STANDOFF", source="AGF-01")
        plan = {"stage": 0}
        for now in (100.0, 160.0, 220.0, 280.0):
            op._respond(alarm, plan, now)
        self.assertEqual(calls.count("radio_yield"), 2)


class FieldDeadlockTests(unittest.TestCase):
    def test_a_robot_stopped_by_its_field_in_front_of_a_waiting_robot_closes_a_deadlock(self):
        # AGF-02 holds the node AGF-01 needs to turn; AGF-01 stands in AGF-02's protective field
        from fleet.traffic import WaitInfo
        sim = quiet_dc(agf=2)
        m = sim.manager
        a, b = m.robots["AGF-01"], m.robots["AGF-02"]
        a.x, a.y, b.x, b.y, b.theta = 10.0, 48.4, 13.3, 48.4, math.pi        # b faces west, a 3.3 m ahead
        b.safety_field, b.driving, b.wait = "PROTECTIVE", False, None
        a.wait = WaitInfo(m.now - 10.0, "robot", node_res("BL11_5"), "AGF-02")
        self.assertEqual(m._field_blocker(b), "AGF-01")
        m._deadlocks()
        m.now += m.cfg.deadlock_confirm_s + 0.5
        m._deadlocks()
        self.assertIn(frozenset({"AGF-01", "AGF-02"}), m.deadlocks)
        b.theta = 0.0                                       # facing away: not what stops it
        self.assertIsNone(m._field_blocker(b))


class PickerTests(unittest.TestCase):
    def test_a_picker_leaves_a_robot_that_is_done_even_if_it_cannot_drive_off(self):
        # the robot behind in a one-way pick aisle is done but stands still: the robot ahead waits for this picker
        from fleet.agents import Walker
        sim = quiet_dc(pickers=1, amr=1)
        (picker,) = [h for h in sim.humans if h.role == "picker"]
        picker.serving, picker.walker = "AMR-01", Walker([])
        robots = {"AMR-01": {"x": picker.x + 0.8, "y": picker.y, "waiting": True}}
        self.assertTrue(picker.picker_update(100.0, sim.layout, robots))
        robots["AMR-01"]["waiting"] = False                 # picked; still standing beside the picker
        for k in range(1, 31):                              # every simulation step for 3 s
            picker.picker_update(100.0 + 0.1 * k, sim.layout, robots)
        self.assertIsNone(picker.serving)


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
