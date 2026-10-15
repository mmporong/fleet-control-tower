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


class TruckLateralClearanceTests(unittest.TestCase):
    def truck(self):
        sim = quiet_dc(rack_forklift=1)
        f = next(f for f in sim.forklifts if f.id == "FL-R1")
        f.idx = f.loop.index("qNE16_2NE22_8")
        f.bx, f.by = sim.layout.nodes[f.loop[f.idx]].pos
        f.heading = 0.0
        f.lat, f.lat_target = 0.3, 1.3
        f.keep_offset_until = 100.0
        f._place(f.heading)
        return f

    def test_a_truck_can_step_sideways_away_from_a_close_robot(self):
        f = self.truck()
        obstacle = ("AGF-01", f.x - 2.7, f.y - 0.3, 0.57, 0.0)
        before = min(math.hypot(obstacle[1] - x, obstacle[2] - y) - r - obstacle[3]
                     for x, y, r in f.circles())
        old_y = f.y
        f.step(0.1, 20.0, [obstacle], [])
        self.assertGreater(f.y, old_y)
        after = min(math.hypot(obstacle[1] - x, obstacle[2] - y) - r - obstacle[3]
                    for x, y, r in f.circles())
        self.assertGreater(after, before)

    def test_a_truck_does_not_step_sideways_toward_a_person(self):
        from fleet.agents import Human
        f = self.truck()
        person = Human("H", "walker", f.x, f.y + 1.0, random.Random(1))
        before = f.pos
        for k in range(5):
            f.step(0.1, 20.0 + k * 0.1, [], [person])
        self.assertEqual(f.pos, before)

    def test_a_truck_can_step_sideways_away_from_a_person(self):
        from fleet.agents import Human
        f = self.truck()
        person = Human("H", "walker", f.x, f.y - 1.0, random.Random(1))
        before = f.y
        f.step(0.1, 20.0, [], [person])
        self.assertGreater(f.y, before)

    def test_a_passing_truck_chooses_the_side_without_a_person(self):
        from fleet.agents import Human
        f = self.truck()
        f.lat = f.lat_target = 0.0
        f._place(f.heading)
        f.blocked_since = 0.0
        robot = ("R", f.x + 3.0, f.y, 0.5, 0.0)
        person = Human("H", "walker", f.x, f.y + 1.3, random.Random(1))
        f.step(0.1, 20.0, [robot], [person])
        self.assertLess(f.lat_target, 0.0)

    def test_a_truck_does_not_step_sideways_toward_a_close_robot(self):
        f = self.truck()
        obstacle = ("AGF-01", f.x - 2.7, f.y + 0.3, 0.57, 0.0)
        before = f.pos
        f.step(0.1, 20.0, [obstacle], [])
        self.assertEqual(f.pos, before)
        self.assertEqual(f.v, 0.0)


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
    def field_pair(self):
        sim = quiet_dc(agf=2)
        m = sim.manager
        a, b = m.robots["AGF-01"], m.robots["AGF-02"]
        a.x, a.y, a.theta = 10.0, 48.4, 0.0
        b.x, b.y = 13.3, 48.4
        a.safety_field, a.driving, a.charging = "PROTECTIVE", False, False
        a.route = [a.last_node, "BL10"]
        a.cur_idx = a.base_end = 0
        return m, a, b

    def test_a_reported_person_or_truck_is_not_a_robot_deadlock(self):
        m, a, _b = self.field_pair()
        for cause in ("human:H1", "forklift:FL-R1", "external:EXT-1"):
            with self.subTest(cause=cause):
                a.field_cause = cause
                self.assertIsNone(m._field_blocker(a))

    def test_standard_field_violation_still_uses_pose_estimates(self):
        m, a, b = self.field_pair()
        a.field_cause = ""
        self.assertEqual(m._field_blocker(a), b.serial)

    def test_a_named_blocking_robot_is_used_during_a_turn(self):
        m, a, b = self.field_pair()
        a.theta = math.pi
        a.field_cause = f"amr:{b.serial}"
        self.assertEqual(m._field_blocker(a), b.serial)

    def test_charging_working_or_idle_robots_do_not_start_field_waits(self):
        for state in ("charging", "action", "idle"):
            with self.subTest(state=state):
                m, a, _b = self.field_pair()
                if state == "charging":
                    a.charging = True
                elif state == "action":
                    a.goal_actions = {0: [{"actionId": "pick"}]}
                    a.actions = {"pick": "RUNNING"}
                else:
                    a.route = []
                self.assertIsNone(m._field_blocker(a))

    def assert_resolver_keeps_unsafe_order(self, configure):
        sim = quiet_dc()
        m = sim.manager
        rec = m.robots["AGF-01"]
        start = rec.last_node
        goal = next(e.end for e in sim.layout.out_edges[start] if sim.layout.allowed(e, rec.vclass))
        rec.route = [start, goal]
        rec.order_id = rec.state_order_id = "order-in-progress"
        rec.cur_idx = rec.base_end = rec.sent_base_end = 0
        rec.driving, rec.dist_since, rec.charging = False, 0.0, False
        rec.task_id, rec.wait = None, None
        rec.goal_actions, rec.actions = {}, {}
        configure(rec)
        self.assertFalse(m.can_take_route(rec) and not rec.charging)
        replanned = []
        m._replan = lambda victim, *args, **kwargs: replanned.append(victim.serial) or True
        m._evasion_spot = lambda *args, **kwargs: None
        self.assertIsNone(m._resolve([rec]))
        self.assertEqual(replanned, [])

    def test_deadlock_resolver_does_not_replace_an_order_mid_edge(self):
        self.assert_resolver_keeps_unsafe_order(lambda rec: setattr(rec, "dist_since", 0.5))

    def test_deadlock_resolver_does_not_move_a_charging_robot(self):
        self.assert_resolver_keeps_unsafe_order(lambda rec: setattr(rec, "charging", True))

    def test_deadlock_resolver_does_not_interrupt_a_goal_action(self):
        def goal_action(rec):
            rec.goal_actions = {1: [{"actionId": "pick-running"}]}
            rec.actions = {"pick-running": "RUNNING"}

        self.assert_resolver_keeps_unsafe_order(goal_action)

    def test_a_robot_stopped_by_its_field_in_front_of_a_waiting_robot_closes_a_deadlock(self):
        # AGF-02 holds the node AGF-01 needs to turn; AGF-01 stands in AGF-02's protective field
        from fleet.traffic import WaitInfo
        sim = quiet_dc(agf=2)
        m = sim.manager
        a, b = m.robots["AGF-01"], m.robots["AGF-02"]
        a.x, a.y, b.x, b.y, b.theta = 10.0, 48.4, 13.3, 48.4, math.pi        # b faces west, a 3.3 m ahead
        b.safety_field, b.driving, b.wait = "PROTECTIVE", False, None
        b.route, b.cur_idx, b.base_end = [b.last_node, "BL10"], 0, 0
        a.wait = WaitInfo(m.now - 10.0, "robot", node_res("BL11_5"), "AGF-02")
        self.assertEqual(m._field_blocker(b), "AGF-01")
        m._deadlocks()
        m.now += m.cfg.deadlock_confirm_s + 0.5
        m._deadlocks()
        self.assertIn(frozenset({"AGF-01", "AGF-02"}), m.deadlocks)
        b.theta = 0.0                                       # facing away: not what stops it
        self.assertIsNone(m._field_blocker(b))


class StandoffEvasionTests(unittest.TestCase):
    @staticmethod
    def ready_at_base_end(rec):
        start = rec.last_node
        rec.route = [start]
        rec.order_id = rec.state_order_id = "order-at-base-end"
        rec.cur_idx = rec.base_end = rec.sent_base_end = 0
        rec.driving, rec.dist_since, rec.charging = False, 0.0, False
        rec.goal_actions, rec.actions = {}, {}

    def test_stopped_truck_neighbourhood_is_not_made_impassable(self):
        from fleet.traffic import WaitInfo
        sim = quiet_dc()
        m = sim.manager
        rec = m.robots["AGF-01"]
        self.ready_at_base_end(rec)
        tid = confirmed_track(m, "forklift", rec.x + 4.0, rec.y)
        rec.wait = WaitInfo(m.now - 5.0, "forklift", node_res(rec.last_node), tid)
        seen = []
        m._evasion_spot = lambda _rec, forbidden, impassable=None: seen.append(
            (set(forbidden), set(impassable or ()))) or None

        m._yield_to_uncontrolled()

        self.assertTrue(seen[0][0])
        self.assertEqual(seen[0][1], set())

    def test_an_actual_truck_block_is_still_impassable(self):
        sim = quiet_dc()
        m = sim.manager
        rec = m.robots["AGF-01"]
        rec.last_node = "qNE16_2NE22_8"
        rec.route = [rec.last_node, "OUT"]
        self.assertIsNotNone(m._evasion_spot(rec, set()))
        m.blocks = {node_res("NE22_8")}
        self.assertIsNone(m._evasion_spot(rec, set()))


    def released_order(self):
        from fleet.dispatcher import Stop, Task
        sim = quiet_dc()
        m = sim.manager
        rec = m.robots["AGF-01"]
        act = {"actionId": "drop-at-goal", "actionType": "drop", "blockingType": "HARD"}
        self.assertTrue(m.assign_route(rec, "R4-01", "dropoff", [act], "T-loaded"))
        rec.state_order_id = rec.order_id
        rec.state_remaining = 3
        rec.base_end = min(3, len(rec.route) - 1)
        rec.sent_base_end = rec.base_end
        rec.loaded = True
        held = node_res(rec.route[rec.base_end])
        m.locks.force(rec.serial, held)
        sim.dispatcher.tasks["T-loaded"] = Task(
            "T-loaded", "PALLET", "FORKLIFT", [Stop(rec.goal, "drop")], 0, m.now,
            state="ACTIVE", robot=rec.serial, assigned_at=m.now, started_at=m.now - 10.0,
        )
        return sim, m, rec, held

    def test_a_standoff_cancel_keeps_the_released_base_until_the_vehicle_confirms(self):
        _sim, m, rec, held = self.released_order()
        old_order, old_route = rec.order_id, list(rec.route)
        sent = []
        persisted = []
        m.on_order = lambda saved: persisted.append(m.robot_snapshot(saved))
        m.instant = lambda serial, kind, params=None, action_id=None: sent.append((serial, kind))

        self.assertTrue(m._cancel_for_standoff(rec, set()))
        self.assertEqual((persisted[0]["purpose"], len(persisted[0]["resume"])), ("recover", 6))
        self.assertEqual((rec.order_id, rec.route), (old_order, old_route))
        self.assertEqual(rec.purpose, "recover")
        self.assertEqual(rec.resume[:2], (old_route[-1], "dropoff"))
        self.assertEqual(m.locks.holder_of(held), rec.serial)
        rec.order_pending = True
        base_end = rec.base_end
        m._extend(rec)
        self.assertEqual(rec.base_end, base_end)       # no extension while cancellation is unconfirmed
        orders = []
        m._send_order = lambda candidate: orders.append(candidate.serial)
        m.tick(m.now + 0.2, 0.2)
        self.assertEqual(orders, [])                   # no order update may race the cancel request
        m.now += 4.0
        m._confirm_cancels()                         # delayed state: resend, do not release or replan
        self.assertEqual(sent, [(rec.serial, "cancelOrder"), (rec.serial, "cancelOrder")])
        self.assertEqual((rec.order_id, rec.route), (old_order, old_route))
        self.assertEqual(m.locks.holder_of(held), rec.serial)

        rec.connection = "OFFLINE"
        m.now += 4.0
        m._confirm_cancels()                         # no radio retries while the vehicle is unreachable
        self.assertEqual(len(sent), 2)
        self.assertEqual(m.locks.holder_of(held), rec.serial)

    def test_a_confirmed_standoff_cancel_resumes_the_same_loaded_task(self):
        _sim, m, rec, held = self.released_order()
        old_order, goal = rec.order_id, rec.goal
        goal_actions = list(rec.goal_actions[len(rec.route) - 1])
        old_action_id = goal_actions[0]["actionId"]
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))

        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1                 # a state received after the cancel request
        m._confirm_cancels()

        self.assertNotEqual(rec.order_id, old_order)
        self.assertEqual((rec.goal, rec.purpose, rec.task_id, rec.loaded),
                         (goal, "dropoff", "T-loaded", True))
        resumed = rec.goal_actions[len(rec.route) - 1]
        self.assertEqual(resumed[0]["actionType"], goal_actions[0]["actionType"])
        self.assertNotEqual(resumed[0]["actionId"], old_action_id)
        self.assertNotIn(rec.serial, m.cancel_pending)
        self.assertIsNone(m.locks.holder_of(held))
        self.assertEqual(m.locks.holder_of(node_res(rec.last_node)), rec.serial)

    def test_a_confirmed_recovery_does_not_erase_its_fallback_evasion_route(self):
        _sim, m, rec, _held = self.released_order()
        original_goal = rec.goal
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, {"NE22_8"}))
        recovery = m._standoff_recovery[rec.serial]
        real_assign = m.assign_route

        direct_allowed = [False]

        def no_direct_recovery(candidate, goal, purpose, *args, **kwargs):
            if not direct_allowed[0] and goal == original_goal and purpose == "dropoff":
                return False
            return real_assign(candidate, goal, purpose, *args, **kwargs)

        m.assign_route = no_direct_recovery
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        self.assertTrue(recovery["confirmed"])
        self.assertEqual(rec.route, [])

        self.assertTrue(real_assign(rec, "qNW10NW16_2", "evade", task_id=rec.task_id))
        fallback = list(rec.route)
        sent = []
        m._send_order = lambda candidate: sent.append(candidate.serial)
        m.mode = "HOLD"
        m.tick(m.now + 0.2, 0.2)

        self.assertEqual(rec.route, fallback)
        self.assertEqual(rec.purpose, "evade")
        self.assertEqual(sent, [])
        m.mode = "RUNNING"
        m.tick(m.now + 0.2, 0.2)
        self.assertEqual(sent, [rec.serial])
        self.assertIn(rec.serial, m._standoff_recovery)

        direct_allowed[0] = True
        rec.state_order_id = rec.order_id
        rec.state_remaining = 0
        rec.cur_idx = len(rec.route) - 1
        rec.last_node = rec.route[-1]
        rec.driving, rec.dist_since = False, 0.0
        m._confirm_cancels()

        self.assertEqual((rec.goal, rec.purpose, rec.task_id),
                         (original_goal, "dropoff", "T-loaded"))
        self.assertNotIn(rec.serial, m._standoff_recovery)

    def test_a_restart_continues_a_confirmed_standoff_evasion_before_resuming(self):
        _sim, m, rec, _held = self.released_order()
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, {"NE22_8"}))
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        recovery = m._standoff_recovery[rec.serial]
        recovery["confirmed"] = True
        m.cancel_pending.pop(rec.serial, None)
        m.cancel_action.pop(rec.serial, None)
        self.assertTrue(m.assign_route(rec, "qNW10NW16_2", "evade", task_id=rec.task_id))
        evasion_order, evasion_route = rec.order_id, list(rec.route)
        snap = m.robot_snapshot(rec)

        restored = FleetManager(m.layout, Bus(random.Random(97)))
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = evasion_route[0], evasion_order
        rr.state_remaining, rr.driving, rr.dist_since = 3, True, 0.2
        restored._reconcile(rr)

        self.assertEqual((rr.purpose, rr.route), ("evade", evasion_route))
        self.assertIn(rr.serial, restored._standoff_recovery)
        self.assertTrue(restored._standoff_recovery[rr.serial]["confirmed"])
        self.assertNotIn(rr.serial, restored.cancel_pending)

    def test_a_restart_does_not_mistake_an_ordinary_evasion_for_standoff_recovery(self):
        _sim, m, rec, _held = self.released_order()
        original = (rec.goal, rec.purpose, list(rec.goal_actions[len(rec.route) - 1]), rec.task_id)
        self.assertTrue(m.assign_route(rec, "qNW10NW16_2", "evade", task_id=rec.task_id))
        rec.resume = original
        snap = m.robot_snapshot(rec)

        restored = FleetManager(m.layout, Bus(random.Random(96)))
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.route[0], rec.order_id
        rr.state_remaining, rr.driving, rr.dist_since = 3, True, 0.2
        restored._reconcile(rr)

        self.assertEqual((rr.purpose, rr.resume), ("evade", original))
        self.assertNotIn(rr.serial, restored._standoff_recovery)
        self.assertNotIn(rr.serial, restored.robot_paused)

    def test_a_stale_zero_remaining_state_does_not_confirm_the_cancel(self):
        _sim, m, rec, held = self.released_order()
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.state_remaining = 0                         # no newer state was received
        m._confirm_cancels()
        self.assertIn(rec.serial, m.cancel_pending)
        self.assertEqual(m.locks.holder_of(held), rec.serial)

    def test_a_different_actual_order_keeps_the_old_order_frozen(self):
        _sim, m, rec, _held = self.released_order()
        old_order = rec.order_id
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.order_pending = False
        rec.last_order = {"orderId": old_order, "orderUpdateId": rec.update_id}
        rec.last_order_at = -10.0
        rec.state_order_id = "vendor-other-order"
        rec.state_remaining = 2
        rec.last_seen = m.now + 0.1
        published = []
        m.bus.publish = lambda topic, payload, now: published.append((topic, payload)) or True

        m.tick(m.now + 0.2, 0.2)

        recovery = m._standoff_recovery[rec.serial]
        self.assertTrue(recovery["blocked"])
        self.assertIn(rec.serial, m.robot_paused)
        self.assertFalse(any("orderId" in payload for _topic, payload in published))

    def test_a_failed_pre_cancel_snapshot_does_not_send_the_cancel(self):
        _sim, m, rec, _held = self.released_order()
        sent = []
        m.on_order = lambda _saved: setattr(m, "mode", "HOLD")
        m.instant = lambda serial, kind, params=None, action_id=None: sent.append((serial, kind))
        self.assertFalse(m._cancel_for_standoff(rec, set()))
        self.assertEqual(sent, [])
        self.assertNotIn(rec.serial, m.cancel_pending)
        self.assertEqual((rec.purpose, rec.resume), ("dropoff", None))

    def test_finished_actions_are_not_replayed_and_failed_actions_block_recovery(self):
        _sim, m, rec, _held = self.released_order()
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        rec.actions[action_id] = "FINISHED"
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        self.assertEqual(rec.goal_actions, {})

        _sim, m, rec, _held = self.released_order()
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        rec.actions[action_id] = "FAILED"
        self.assertFalse(m._cancel_for_standoff(rec, set()))

    def test_an_action_that_finishes_while_cancel_is_pending_is_not_scheduled_twice(self):
        _sim, m, rec, _held = self.released_order()
        old_order = rec.order_id
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.actions[action_id] = "FINISHED"
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        self.assertNotEqual(rec.order_id, old_order)
        self.assertEqual(rec.goal_actions, {})

    def test_an_operator_reassignment_ends_recovery_then_the_generic_cancel_clears_the_route(self):
        sim, m, rec, held = self.released_order()
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        sim.dispatcher.reassign("T-loaded")
        self.assertIsNone(rec.task_id)
        self.assertNotIn("T-loaded", sim.dispatcher.tasks)
        self.assertNotIn(rec.serial, m._standoff_recovery)
        self.assertIsNone(rec.resume)
        self.assertIn(rec.serial, m.cancel_pending)
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1

        m._confirm_cancels()
        sim.dispatcher._advance_job(rec, m.now)
        m._progress(rec)

        self.assertNotIn(rec.serial, m.cancel_pending)
        self.assertEqual((rec.route, rec.purpose), ([], ""))
        self.assertIsNone(m.locks.holder_of(held))

    def test_a_generic_detach_tracks_one_cancel_and_does_not_replace_its_running_action(self):
        sim, m, rec, _held = self.released_order()
        sent = []

        def send(_serial, kind, _params=None, action_id=None):
            sent.append(action_id)
            return action_id

        m.instant = send
        sim.dispatcher.detach(rec, "고장 장기화")
        first = sent[0]
        self.assertEqual(m.cancel_pending[rec.serial][0], rec.state_order_id)
        self.assertEqual(m.cancel_action[rec.serial], first)

        sim.dispatcher.detach(rec, "고장 장기화")
        self.assertEqual(sent, [first])
        rec.actions[first] = "RUNNING"
        m.now += 4.0
        m._confirm_cancels()
        self.assertEqual(sent, [first])

        rec.actions[first] = "FAILED"
        m.now += 0.2
        m._confirm_cancels()
        self.assertEqual(sent, [first, "cancelOrder-2"])
        self.assertEqual(m.cancel_action[rec.serial], "cancelOrder-2")

    def test_a_generic_detach_needs_a_new_stopped_state_before_releasing_its_route(self):
        sim, m, rec, held = self.released_order()
        m.instant = lambda *_args, **_kwargs: "cancel-generic"
        sim.dispatcher.detach(rec, "고장 장기화")
        _order_id, sent = m.cancel_pending[rec.serial]
        rec.state_remaining = 0
        rec.driving, rec.dist_since = False, 0.0
        rec.last_seen = sent                    # the zero state predates the cancel request

        m._confirm_cancels()
        sim.dispatcher._advance_job(rec, m.now)
        m._progress(rec)

        self.assertIn(rec.serial, m.cancel_pending)
        self.assertEqual(m.locks.holder_of(held), rec.serial)

    def test_a_generic_detach_holds_and_cancels_a_new_actual_order(self):
        sim, m, rec, _held = self.released_order()
        sent = []
        m.instant = lambda _serial, kind, _params=None, action_id=None: sent.append(kind) or action_id
        sim.dispatcher.detach(rec, "고장 장기화")
        reported = rec.route[min(1, len(rec.route) - 1)]
        rec.state_order_id = "vendor-new-order"
        rec.state_remaining = 4
        rec.state_nodes = [(reported, 2, True)]
        rec.last_seen = m.now + 0.1

        m._confirm_cancels()

        self.assertEqual(m.locks.holder_of(node_res(reported)), rec.serial)
        self.assertIn(rec.serial, m.robot_paused)
        self.assertTrue(m.alarms.is_active(f"RESERVATION_MISMATCH:{rec.serial}"))
        self.assertEqual(m.cancel_pending[rec.serial][0], "vendor-new-order")
        self.assertEqual(sent, ["cancelOrder", "cancelOrder"])

    def test_a_restart_reissues_a_persisted_generic_detach_cancel(self):
        sim, m, rec, _held = self.released_order()
        m.instant = lambda *_args, **_kwargs: "cancel-before-restart"
        sim.dispatcher.detach(rec, "고장 장기화")
        snap = m.robot_snapshot(rec)
        self.assertEqual((snap["cancel_order_id"], snap["cancel_action_id"]),
                         (rec.state_order_id, m.cancel_action[rec.serial]))

        restored = FleetManager(m.layout, Bus(random.Random(95)))
        restored.run_id = "after-"
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, rec.state_order_id
        rr.state_remaining, rr.driving, rr.dist_since = 3, False, 0.2
        sent = []
        restored.instant = lambda serial, kind, params=None, action_id=None: \
            sent.append((serial, kind)) or action_id
        restored._reconcile(rr)

        self.assertEqual(sent, [(rec.serial, "cancelOrder")])
        self.assertEqual(restored.cancel_pending[rec.serial][0], rec.state_order_id)
        self.assertEqual(restored.cancel_action[rec.serial], "after-cancelOrder-1")
        self.assertNotEqual(restored.cancel_action[rec.serial], snap["cancel_action_id"])
        self.assertEqual((rr.purpose, rr.task_id), ("recover", None))

    def test_a_restart_keeps_a_running_persisted_cancel_without_replacing_it(self):
        sim, m, rec, _held = self.released_order()
        m.instant = lambda *_args, **_kwargs: "cancel-before-restart"
        sim.dispatcher.detach(rec, "고장 장기화")
        snap = m.robot_snapshot(rec)

        restored = FleetManager(m.layout, Bus(random.Random(951)))
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, rec.state_order_id
        rr.state_remaining, rr.driving, rr.dist_since = 3, False, 0.2
        rr.actions[snap["cancel_action_id"]] = "RUNNING"
        sent = []
        restored.instant = lambda serial, kind, params=None, action_id=None: \
            sent.append((serial, kind)) or action_id

        restored._reconcile(rr)

        self.assertEqual(sent, [])
        self.assertEqual(restored.cancel_pending[rec.serial][0], rec.state_order_id)
        self.assertEqual(restored.cancel_action[rec.serial], snap["cancel_action_id"])

    def test_a_restart_keeps_the_running_cancel_for_a_new_actual_order(self):
        sim, m, rec, _held = self.released_order()
        sim.dispatcher.detach(rec, "고장 장기화")
        reported = rec.route[min(1, len(rec.route) - 1)]
        rec.state_order_id = "vendor-new-order"
        rec.state_remaining = 4
        rec.state_nodes = [(reported, 2, True)]
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        persisted_action = m.cancel_action[rec.serial]
        snap = m.robot_snapshot(rec)

        restored = FleetManager(m.layout, Bus(random.Random(952)))
        restored.run_id = "after-"
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, "vendor-new-order"
        rr.state_remaining, rr.driving, rr.dist_since = 4, False, 0.2
        rr.state_nodes = [(reported, 2, True)]
        rr.actions[persisted_action] = "RUNNING"
        sent = []
        restored.instant = lambda serial, kind, params=None, action_id=None: \
            sent.append((serial, kind, action_id)) or action_id

        restored._reconcile(rr)

        self.assertEqual(sent, [])
        self.assertEqual(restored.cancel_pending[rec.serial][0], "vendor-new-order")
        self.assertEqual(restored.cancel_action[rec.serial], persisted_action)

    def test_only_a_cancel_failed_action_is_reissued_after_confirmation(self):
        _sim, m, rec, _held = self.released_order()
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.actions[action_id] = "FAILED"
        rec.action_results[action_id] = "cancelOrder"
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        resumed = rec.goal_actions[len(rec.route) - 1][0]
        self.assertNotEqual(resumed["actionId"], action_id)

        _sim, m, rec, _held = self.released_order()
        old_order = rec.order_id
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.actions[action_id] = "FAILED"
        rec.action_results[action_id] = "station error"
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        self.assertEqual(rec.order_id, old_order)
        self.assertIn(rec.serial, m.robot_paused)

    def test_a_blocked_action_recovery_stays_blocked_after_a_restart(self):
        _sim, m, rec, held = self.released_order()
        old_order = rec.order_id
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        snapshots = []
        m.on_order = lambda saved: snapshots.append(m.robot_snapshot(saved))
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.actions[action_id] = "FAILED"
        rec.action_results[action_id] = "station error"
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        snap = snapshots[-1]
        self.assertIn("동작 상태 불일치", snap["resume"][5])

        restored = FleetManager(m.layout, Bus(random.Random(98)))
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, old_order
        rr.state_remaining, rr.driving, rr.dist_since = 0, False, 0.0
        rr.actions = {}
        restored._reconcile(rr)
        restored.mode = "RUNNING"
        restored._confirm_cancels()

        self.assertEqual(rr.order_id, old_order)
        self.assertTrue(restored._standoff_recovery[rr.serial]["blocked"])
        self.assertIn(rr.serial, restored.robot_paused)
        self.assertIsNone(restored.locks.holder_of(held))
        self.assertEqual(restored.locks.holder_of(node_res(rr.last_node)), rr.serial)

    def test_the_loaded_task_finishes_once_only_after_the_new_goal_action(self):
        from types import SimpleNamespace
        from fleet.dispatcher import Stop, Task
        sim, m, rec, _held = self.released_order()
        d = sim.dispatcher
        task = Task("T-loaded", "PALLET", "FORKLIFT", [Stop(rec.goal, "drop")], 0, m.now,
                    state="ACTIVE", robot=rec.serial, assigned_at=m.now, started_at=m.now - 10.0)
        d.tasks[task.id] = task
        events = []
        d.process = SimpleNamespace(
            on_stop_done=lambda *_args: events.append("stop"),
            on_task_end=lambda *_args: events.append("end"),
        )
        m.instant = lambda *_args, **_kwargs: None
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m._confirm_cancels()
        action_id = rec.goal_actions[len(rec.route) - 1][0]["actionId"]
        rec.state_order_id = rec.order_id
        rec.cur_idx = rec.base_end = len(rec.route) - 1
        rec.last_node, rec.driving, rec.dist_since = rec.goal, False, 0.0
        rec.actions[action_id] = "WAITING"
        d._advance_job(rec, m.now)
        self.assertIn(task.id, d.tasks)
        self.assertEqual(events, [])

        rec.actions[action_id] = "FINISHED"
        d._advance_job(rec, m.now)
        d._advance_job(rec, m.now)
        self.assertNotIn(task.id, d.tasks)
        self.assertEqual(events, ["stop", "end"])

    def test_a_restart_continues_the_persisted_cancel_before_resuming_the_task(self):
        _sim, m, rec, held = self.released_order()
        m.instant = lambda *_args, **_kwargs: None
        avoid = {"NE22_8"}
        self.assertIn(next(iter(avoid)), rec.route)
        self.assertTrue(m._cancel_for_standoff(rec, avoid))
        snap = m.robot_snapshot(rec)
        self.assertEqual((snap["purpose"], len(snap["resume"]), set(snap["resume"][4])),
                         ("recover", 6, avoid))

        restored = FleetManager(m.layout, Bus(random.Random(99)))
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, rec.order_id
        rr.state_remaining, rr.driving, rr.dist_since, rr.loaded = 0, False, 0.0, True
        restored._reconcile(rr)

        self.assertEqual((rr.order_id, rr.purpose), (snap["order_id"], "recover"))
        self.assertIn(rr.serial, restored._standoff_recovery)
        self.assertEqual(restored.locks.holder_of(held), rr.serial)
        restored.mode = "RUNNING"
        restored._confirm_cancels()

        self.assertEqual((rr.goal, rr.purpose, rr.task_id, rr.loaded),
                         (snap["resume"][0], "dropoff", "T-loaded", True))
        self.assertTrue(avoid.isdisjoint(rr.route))
        self.assertIsNone(restored.locks.holder_of(held))
        self.assertEqual(restored.locks.holder_of(node_res(rr.last_node)), rr.serial)

    def test_a_restart_keeps_a_running_standoff_cancel_without_replacing_it(self):
        _sim, m, rec, _held = self.released_order()
        self.assertTrue(m._cancel_for_standoff(rec, {"NE22_8"}))
        snap = m.robot_snapshot(rec)
        persisted_action = snap["cancel_action_id"]

        restored = FleetManager(m.layout, Bus(random.Random(991)))
        restored.run_id = "after-"
        restored.restore({rec.serial: snap})
        rr = restored.robots[rec.serial]
        rr.has_state, rr.connection, rr.mode = True, "ONLINE", "AUTOMATIC"
        rr.last_node, rr.state_order_id = rec.last_node, rec.order_id
        rr.state_remaining, rr.driving, rr.dist_since, rr.loaded = 3, False, 0.0, True
        rr.actions[persisted_action] = "RUNNING"
        sent = []
        restored.instant = lambda serial, kind, params=None, action_id=None: \
            sent.append((serial, kind, action_id)) or action_id

        restored._reconcile(rr)

        self.assertEqual(sent, [])
        self.assertEqual(restored.cancel_pending[rec.serial][0], rec.order_id)
        self.assertEqual(restored.cancel_action[rec.serial], persisted_action)
        self.assertIn(rec.serial, restored._standoff_recovery)

    def test_a_confirmed_recovery_waits_for_running_mode_before_replanning_or_sending(self):
        _sim, m, rec, held = self.released_order()
        old_order = rec.order_id
        sent = []
        m.instant = lambda *_args, **_kwargs: None
        m._send_order = lambda candidate: sent.append(candidate.order_id)
        self.assertTrue(m._cancel_for_standoff(rec, set()))
        rec.state_remaining = 0
        rec.last_seen = m.now + 0.1
        m.mode = "HOLD"

        m._confirm_cancels()
        self.assertEqual((rec.order_id, rec.purpose), (old_order, "recover"))
        self.assertIn(rec.serial, m._standoff_recovery)
        self.assertEqual(m.locks.holder_of(held), rec.serial)
        m.tick(m.now + 0.2, 0.2)
        self.assertEqual(sent, [])

        m.mode = "RUNNING"
        m._confirm_cancels()
        self.assertNotEqual(rec.order_id, old_order)
        self.assertNotIn(rec.serial, m._standoff_recovery)
        self.assertIsNone(m.locks.holder_of(held))

    def test_only_a_long_node_standoff_without_a_running_action_can_be_cancelled(self):
        _sim, m, rec, _held = self.released_order()
        tid = confirmed_track(m, "forklift", rec.x + 2.0, rec.y)
        truck = m.tracker.tracks[tid]
        rec.safety_field, rec.field_cause = "PROTECTIVE", "forklift:FL-R1"
        rec.protective_since = m.now - m.cfg.standoff_alarm_s - 1.0
        rec.driving, rec.dist_since, rec.charging = False, 0.0, False
        self.assertIs(m._released_standoff_truck(rec, [truck]), truck)
        for change in (
                lambda: setattr(rec, "protective_since", m.now - m.cfg.standoff_alarm_s + 1.0),
                lambda: setattr(rec, "dist_since", 0.1),
                lambda: rec.actions.update({"drop-at-goal": "RUNNING"})):
            rec.protective_since = m.now - m.cfg.standoff_alarm_s - 1.0
            rec.dist_since = 0.0
            rec.actions.clear()
            change()
            self.assertIsNone(m._released_standoff_truck(rec, [truck]))

    def test_an_unloaded_active_task_can_recover_but_an_unassigned_robot_cannot(self):
        _sim, m, rec, _held = self.released_order()
        tid = confirmed_track(m, "forklift", rec.x + 2.0, rec.y)
        truck = m.tracker.tracks[tid]
        rec.safety_field, rec.field_cause = "PROTECTIVE", "forklift:FL-R1"
        rec.protective_since = m.now - m.cfg.standoff_alarm_s - 1.0
        rec.driving, rec.dist_since, rec.charging = False, 0.0, False
        rec.loaded = False
        self.assertIs(m._released_standoff_truck(rec, [truck]), truck)
        rec.loaded, rec.task_id = True, None
        self.assertIsNone(m._released_standoff_truck(rec, [truck]))


class PickerTests(unittest.TestCase):
    def walking_picker(self):
        from fleet.agents import Walker
        sim = quiet_dc(pickers=1, amr=0)
        (picker,) = [h for h in sim.humans if h.role == "picker"]
        # A moving AMR can leave the picker a few centimetres inside the nominal 0.75 m shelf-face line.
        picker.x, picker.y = 62.70, 14.0
        picker.walker = Walker([(62.70, 19.0)])
        picker.dwell_until = picker.frozen_until = 0.0
        picker.distracted = picker.inattentive = False
        return sim, picker

    def test_a_picker_at_the_shelf_face_passes_a_stationary_aligned_amr(self):
        from fleet.vehicle import tote_amr_spec
        sim, picker = self.walking_picker()
        body = tote_amr_spec().circles(62.0, 16.0, math.pi / 2)
        robots = [("AMR-X", x, y, radius, 0.0) for x, y, radius in body]
        for now in range(1, 5):
            picker.step(1.0, float(now), sim.layout, robots, [])
        self.assertGreater(picker.y, 17.0)

    def test_a_picker_cannot_pass_an_amr_body_turned_across_the_path(self):
        from fleet.vehicle import tote_amr_spec
        sim, picker = self.walking_picker()
        body = tote_amr_spec().circles(62.0, 16.0, 0.0)
        robots = [("AMR-X", x, y, radius, 0.0) for x, y, radius in body]
        for now in range(1, 5):
            picker.step(1.0, float(now), sim.layout, robots, [])
        self.assertLess(picker.y, 16.0)

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
    def test_a_nearby_person_does_not_take_a_stale_forklift_track(self):
        tr = Tracker()
        tr.update([Detection("forklift", 20.0, 6.2, "infra")], 0.0)
        tr.update([Detection("forklift", 20.0, 6.2, "infra")], 0.5)
        (truck_id,) = tr.tracks
        tr.update([], 1.0)
        tr.update([], 1.5)
        tr.update([Detection("human", 20.1, 6.2, "infra"),
                   Detection("forklift", 23.0, 6.2, "infra")], 2.0)
        truck = tr.tracks[truck_id]
        self.assertEqual((truck.cls, truck.hits), ("forklift", 3))
        self.assertGreater(truck.x, 21.0)
        people = [track for track in tr.tracks.values() if track.cls == "human"]
        self.assertEqual(len(people), 1)
        self.assertFalse(people[0].confirmed)

    def test_one_close_misclassification_stays_on_the_existing_track(self):
        tr = Tracker()
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.0)
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.5)
        (truck_id,) = tr.tracks
        tr.update([Detection("human", 10.1, 10.0, "infra")], 1.0)
        self.assertEqual(list(tr.tracks), [truck_id])
        self.assertEqual((tr.tracks[truck_id].cls, tr.tracks[truck_id].hits), ("forklift", 3))

    def test_a_distant_other_class_does_not_use_the_forklift_gate(self):
        tr = Tracker()
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.0)
        tr.update([Detection("forklift", 10.0, 10.0, "infra")], 0.5)
        tr.update([Detection("human", 12.0, 10.0, "infra")], 1.0)
        self.assertEqual(len(tr.tracks), 2)

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


class HumanPredictionTests(unittest.TestCase):
    def test_a_resting_person_does_not_sweep_from_camera_noise(self):
        tr = Tracker()
        rng = random.Random(6)
        for k in range(12):
            tr.update([Detection("human", 5.0 + rng.gauss(0, 0.25),
                                 3.0 + rng.gauss(0, 0.25), "infra")], k * 0.5)
        (person,) = tr.tracks.values()
        self.assertTrue(person.resting)
        self.assertGreater(person.speed, 0.3)                 # filter noise alone crosses STILL_SPEED
        self.assertEqual(len(tr.predicted_discs(5.5, {"human": 2.0})), 1)

    def test_a_person_walking_after_a_stop_is_predicted_again(self):
        tr = Tracker()
        for k in range(8):
            tr.update([Detection("human", 5.0, 3.0, "infra")], k * 0.5)
        for k in range(1, 5):
            tr.update([Detection("human", 5.0 + 0.8 * k * 0.5, 3.0, "infra")], (7 + k) * 0.5)
        (person,) = tr.tracks.values()
        self.assertFalse(person.resting)
        self.assertGreater(len(tr.predicted_discs(5.5, {"human": 2.0})), 1)


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
        rec.last_seen = m.now + 0.1
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
