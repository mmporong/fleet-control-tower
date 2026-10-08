"""Whole-simulation checks: invariants, null models, the DC process, dispatcher and operator commands."""
from __future__ import annotations

import math
import random
import unittest

from fleet.autoop import AutoOperator
from fleet.ops import Operations
from fleet.scenario import get
from fleet.sim import Simulation
from fleet.site_dc import AGF, AMR


def sim_with_operator(name: str, seed: int = 5, **overrides) -> tuple[Simulation, Operations]:
    sim = Simulation(get(name, **overrides), seed=seed)
    ops = Operations(sim)
    sim.tick_hooks.append(AutoOperator(ops, random.Random(seed)).tick)
    return sim, ops


class GridInvariantTests(unittest.TestCase):
    def test_grid_shift_invariants(self):
        sim, _ = sim_with_operator("grid")
        for _ in range(300):
            sim.run(1.0)
            self.assertEqual(sim.manager.locks.check(), [])
        kpi = sim.kpi()
        self.assertGreater(kpi["done"], 0)
        self.assertEqual([c for c in sim.evaluator.contacts if c["kind"] == "amr"], [])
        self.assertEqual(kpi["mismatch"], 0)

    def test_null_model_without_people(self):
        """With no people the people-related detectors must stay silent."""
        sim, _ = sim_with_operator("grid-night")
        sim.run(600)
        kpi = sim.kpi()
        self.assertEqual(kpi["close_calls"], 0)
        self.assertEqual(kpi["clearance_violations"], 0)
        self.assertEqual(kpi["near_miss_fms"], 0)
        self.assertEqual(kpi["contacts_human"], 0)
        self.assertEqual(kpi["protective_human"], 0)
        self.assertIsNone(kpi["tracking_ratio"])

    def test_single_robot_has_no_deadlock(self):
        sim, _ = sim_with_operator("grid-night", robots=1, external=0)
        sim.run(600)
        self.assertEqual(sim.kpi()["deadlocks"], 0)
        self.assertGreater(sim.kpi()["done"], 0)

    def test_deterministic_for_a_seed(self):
        for name in ("grid", "standard"):
            a, _ = sim_with_operator(name, seed=11)
            b, _ = sim_with_operator(name, seed=11)
            a.run(120)
            b.run(120)
            self.assertEqual(a.kpi(), b.kpi(), name)


class DcTests(unittest.TestCase):
    def test_vehicle_classes_come_from_factsheets(self):
        sim = Simulation(get("standard"), seed=2)
        sim.run(2)
        classes = {r.serial: r.vclass for r in sim.manager.robots.values()}
        self.assertTrue(all(c == AGF for s, c in classes.items() if s.startswith("AGF")))
        self.assertTrue(all(c == AMR for s, c in classes.items() if s.startswith("AMR")))
        agf = sim.manager.robots["AGF-01"]
        self.assertEqual(agf.length, 2.7)
        self.assertFalse(sim.manager.supports(agf, "x_pickItems"))

    def test_class_restricted_routes(self):
        sim = Simulation(get("standard"), seed=2)
        sim.run(2)
        L = sim.layout
        p = sim.manager.planner
        agf_path = p.plan("SE10", "SO3", vclass=AGF)
        self.assertTrue(agf_path and not any(L.nodes[n].kind == "pickface" for n in agf_path))
        amr_path = p.plan("PK1", "K1-05", vclass=AMR)
        self.assertTrue(amr_path and not any(L.nodes[n].kind == "rack" for n in amr_path))
        self.assertIsNone(p.plan("PK1", "R1-03", vclass=AMR))

    def test_pick_action_needs_a_picker(self):
        sim = Simulation(get("standard", pickers=0, tote_orders_h=0.0, trucks_h=0.0, out_pallets_h=0.0), seed=4)
        sim.run(2)
        t = sim.dispatcher.add_task("TOTE_PICK", "K3-04", None, 2)
        sim.run(400)
        self.assertEqual(t.state, "ACTIVE")
        rec = sim.manager.robots[t.robot]
        self.assertEqual(rec.last_node, "K3-04")
        self.assertIn("피커 대기", rec.reason)

    def test_dc_shift_invariants_and_pallet_conservation(self):
        sim, _ = sim_with_operator("standard", seed=5)
        p = sim.process
        for _ in range(60):
            sim.run(10.0)
            self.assertEqual(sim.manager.locks.check(), [])
            # every pallet is in exactly one place and the rack table agrees with the pallet table
            in_rack = {pid for pid in p.slots.values() if pid}
            for pid, info in p.pallets.items():
                where = info["loc"][0]
                self.assertEqual(pid in in_rack, where == "rack", pid)
            staged = [pid for lane in p.in_stage.values() for pid in lane]
            self.assertEqual(len(staged), len(set(staged)))
        kpi = sim.kpi()
        site = kpi["site"]
        self.assertGreater(site["putaway"], 0)
        self.assertGreater(site["picks"], 0)
        self.assertGreater(site["pallets_unloaded"], 0)
        self.assertEqual([c for c in sim.evaluator.contacts if c["kind"] == "amr"], [])
        self.assertEqual(kpi["mismatch"], 0)

    def test_night_has_no_picking(self):
        sim, _ = sim_with_operator("night", seed=3)
        sim.run(600)
        site = sim.kpi()["site"]
        self.assertEqual(site["tote_done"], 0)
        self.assertEqual(site["picker_wait_s"], 0.0)
        self.assertGreater(site["putaway"] + site["shipped"], 0)


def lone_agf(seed: int = 3) -> Simulation:
    """Distribution centre with one autonomous forklift and nothing else moving."""
    sc = get("standard", agf=1, amr=0, pickers=0, packers=0, dock_workers=0, walkers=0, dock_forklifts_in=0,
             dock_forklifts_out=0, rack_forklift=0, external=0, trucks_h=0.0, out_pallets_h=0.0,
             tote_orders_h=0.0, fault_mtbf_h=0.0, comms_mtbf_h=0.0)
    sim = Simulation(sc, seed=seed)
    sim.run(2)
    return sim


class StubApproachTests(unittest.TestCase):
    def test_second_forklift_waits_outside_the_turning_circle_of_an_occupied_stub(self):
        sc = get("standard", agf=2, amr=0, pickers=0, packers=0, dock_workers=0, walkers=0, dock_forklifts_in=0,
                 dock_forklifts_out=0, rack_forklift=0, external=0, trucks_h=0.0, out_pallets_h=0.0,
                 tote_orders_h=0.0, fault_mtbf_h=0.0, comms_mtbf_h=0.0)
        sim = Simulation(sc, seed=3)
        sim.run(2)
        m = sim.manager
        first, second = m.robots["AGF-01"], m.robots["AGF-02"]
        self.assertTrue(m.assign_route(first, "SO3", "test"))
        while sim.now < 400 and first.last_node != "SO3":
            sim.run(1)
        self.assertEqual(first.last_node, "SO3")
        self.assertTrue(m.assign_route(second, "SO3", "test"))
        junction = sim.layout.nodes["E21"]
        closest = 99.0
        while sim.now < 800 and not (second.wait and second.wait.resource == "N:SO3"):
            sim.run(1)
        for _ in range(20):                               # it holds position while the stub is taken
            sim.run(1)
            node = sim.layout.nodes[second.last_node]
            closest = min(closest, abs(node.y - junction.y) if abs(node.x - junction.x) < 0.1 else 99.0)
        self.assertGreater(closest, 3.0)                  # not inside the leaving forklift's turning circle
        self.assertTrue(m.assign_route(first, "PA14_3", "test"))   # the first one leaves the stub
        while sim.now < 1400 and second.last_node != "SO3":
            sim.run(1)
        self.assertEqual(second.last_node, "SO3")
        self.assertEqual(m.counters["deadlocks"], 0)


class AisleSignalTests(unittest.TestCase):
    def test_robots_do_not_enter_an_aisle_a_manual_truck_is_working(self):
        from fleet.perception import Detection
        sim = lone_agf()
        m = sim.manager
        m.ingest_detections([Detection("forklift", 16.2, 20.0, "infra")])   # reach truck inside rack aisle R1
        m.tick(sim.now, 0.2)
        self.assertEqual(m.blocks["N:R1-01"][0], "forklift")                 # the whole aisle, not just nearby
        self.assertNotIn("N:R2-01", m.blocks)

    def test_manual_truck_waits_at_the_mouth_while_a_robot_is_in_the_aisle(self):
        from fleet.agents import Forklift, expand_loop
        sim = lone_agf()
        L = sim.layout
        loop = expand_loop(L, ["SW16_2", "R1-01", "R1-09", "NE16_2", "NE22_8", "R2-09", "R2-01", "SW22_8"],
                           {"cross", "queue", "spine", "rack"})
        f = Forklift("FL-X", loop, L, random.Random(1))
        f.heading = math.pi / 2                                              # facing into the aisle
        robot_inside = [("AGF-99", 16.2, 25.0, 0.67, 0.0)]
        t = 0.0
        for _ in range(200):
            f.step(0.1, t, robot_inside, [])
            t += 0.1
        self.assertLess(f.y, 8.6)                                            # still at the mouth
        for _ in range(100):
            f.step(0.1, t, [], [])
            t += 0.1
        self.assertGreater(f.y, 8.6)                                         # aisle clear: drives in


class BayTurnTests(unittest.TestCase):
    def test_leaving_a_bay_does_not_reserve_the_neighbouring_bay(self):
        """A forklift parked in the next bay lies along its stub, clear of the turn out of this bay."""
        sim = lone_agf()
        m = sim.manager
        rec = m.robots["AGF-01"]
        rec.route = ["PA39_5", "BL39_5", "BL36_7", "BL33_9"]
        zone = m._turn_zone(rec, 1)
        self.assertNotIn("N:PA36_7", zone)                  # body 2.7 m away, swept circle 1.7 m
        self.assertIn("N:BL36_7", zone)                     # a lane node: could hold a vehicle facing any way
        self.assertAlmostEqual(m._stub_body_gap("PA39_5", (39.5, 48.4), 2.7, 1.3), 1.65, places=2)


class DoorTests(unittest.TestCase):
    def drive_to(self, sim: Simulation, goal: str, limit: float = 240.0) -> list[str]:
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, goal, "test"))
        lanes = []
        while sim.now < limit + 2 and rec.last_node != goal:
            sim.run(0.5)
            if rec.cur_idx > 0:
                lanes.append(sim.layout.edge_between(rec.route[rec.cur_idx - 1], rec.route[rec.cur_idx]).lane)
        self.assertEqual(rec.last_node, goal)
        return lanes

    def test_door_opens_ahead_of_the_forklift(self):
        sim = lone_agf()
        v = sim.vehicle("AGF-01")
        door_stops = 0
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "SE66", "test"))
        waited = 0.0
        while sim.now < 240 and rec.last_node != "SE66":
            sim.run(0.5)
            door_stops += v.field == "PROTECTIVE" and v.field_cause.startswith("door")
            waited += 0.5 if rec.wait is not None and rec.wait.kind == "door" else 0.0
        self.assertEqual(rec.last_node, "SE66")
        self.assertEqual(door_stops, 0)                   # the curtain never stood in front of the truck
        self.assertLessEqual(waited, 2.0)                 # requested early enough not to stop for it
        self.assertGreaterEqual(sim.manager.doors.view["D-S"].opens, 1)

    def test_jammed_door_raises_alarm_and_traffic_detours(self):
        sim = lone_agf()
        ops = Operations(sim)
        self.assertTrue(ops.execute({"cmd": "drill_door", "door": "D-N", "fault": "JAM"})["ok"])
        lanes = self.drive_to(sim, "NE66", limit=420.0)
        alarm = sim.manager.alarms.alarms.get("FACILITY_FAULT:D-N")
        self.assertIsNotNone(alarm)
        self.assertEqual(sim.manager.doors.view["D-N"].fail_cause, "timeout")
        self.assertFalse(set(lanes) & sim.layout.doors["D-N"].lanes)     # went round through the south door
        self.assertTrue(set(lanes) & sim.layout.doors["D-S"].lanes)
        # after the field fix a retry clears the alarm and the door is planned through again
        ops.execute({"cmd": "door_field_fix", "door": "D-N"})
        ops.execute({"cmd": "door_retry", "door": "D-N"})
        sim.run(1)
        self.assertTrue(sim.manager.doors.available("D-N", sim.now))
        self.assertFalse(sim.manager.alarms.alarms["FACILITY_FAULT:D-N"].active)

    def test_silent_door_controller_is_detected_and_recovers(self):
        sim = lone_agf()
        sim.drill_door("D-S", "NO_RESPONSE")
        sim.run(5)
        self.assertEqual(sim.manager.doors.view["D-S"].fail_cause, "stale")
        self.assertIn("SE42_6|SE50", sim.manager.doors.unavailable_lanes(sim.now))
        sim.door_field_fix("D-S")
        sim.run(3)
        self.assertEqual(sim.manager.doors.view["D-S"].fail_cause, "")   # the door reports again: clears itself

    def test_operator_takes_a_door_out_of_use(self):
        sim = lone_agf()
        ops = Operations(sim)
        ops.execute({"cmd": "door_disable", "door": "D-N"})
        lanes = self.drive_to(sim, "NE66", limit=420.0)
        self.assertFalse(set(lanes) & sim.layout.doors["D-N"].lanes)
        self.assertFalse(ops.execute({"cmd": "door_disable", "door": "D-X"})["ok"])
        ops.execute({"cmd": "door_retry", "door": "D-N"})
        self.assertTrue(sim.manager.doors.available("D-N", sim.now))

    def test_closed_door_names_the_wait_even_with_a_person_on_the_crossing(self):
        from fleet.perception import Detection
        sim = lone_agf()
        m = sim.manager
        lane = "L:SE42_6|SE50"
        m.ingest_detections([Detection("human", 46.6, 4.0, "infra")])   # on the south crossing, beside the door
        m.tick(sim.now, 0.2)
        self.assertEqual(m.blocks[lane][0], "door")         # the door is shut: it is the lasting cause

    def test_virtual_operator_brings_a_door_back_when_the_fault_clears_by_itself(self):
        sim = lone_agf()
        sim.tick_hooks.append(AutoOperator(Operations(sim), random.Random(3)).tick)
        sim.doors["D-S"].inject("NO_RESPONSE", sim.now + 200.0)   # controller back after 200 s
        sim.run(120)
        view = sim.manager.doors.view["D-S"]
        self.assertTrue(view.disabled)                             # SOP: taken out of use
        sim.run(900)                                               # alarm cleared long ago, field fix done
        self.assertFalse(view.disabled)
        self.assertTrue(sim.manager.doors.available("D-S", sim.now))

    def test_disabling_a_door_does_not_shut_it_on_a_vehicle_that_reserved_it(self):
        sim = lone_agf()
        ops = Operations(sim)
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "SE66", "test"))
        lane = "L:SE42_6|SE50"
        while sim.now < 300 and sim.manager.locks.holder_of(lane) != "AGF-01":
            sim.run(0.5)
        self.assertEqual(sim.manager.locks.holder_of(lane), "AGF-01")
        ops.execute({"cmd": "door_disable", "door": "D-S"})
        while sim.now < 500 and rec.last_node != "SE66":
            sim.run(0.5)
        self.assertEqual(rec.last_node, "SE66")                    # let through, not stopped at the curtain

    def test_no_shuttling_between_two_unusable_doors(self):
        sim = lone_agf()
        ops = Operations(sim)
        ops.execute({"cmd": "door_disable", "door": "D-S"})
        ops.execute({"cmd": "door_disable", "door": "D-N"})
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "NE66", "test"))
        sim.run(400)
        self.assertEqual(sim.manager.counters["reroutes"], 0)     # waits at the first door instead
        self.assertIsNotNone(rec.wait)
        self.assertEqual(rec.wait.kind, "door")
        alarm = sim.manager.alarms.alarms.get("BLOCKED_LONG:AGF-01")
        self.assertTrue(alarm is not None and alarm.active)        # no door alarm covers it: raised
        self.assertIn("사용 중지", alarm.message)

    def test_outbound_stays_reachable_through_either_door(self):
        """The door drill found the south door to be a single point of failure for outbound staging; the
        east return lane removes it: with either door out an AGF still reaches every outbound lane."""
        sim = lone_agf()
        for door in ("D-S", "D-N"):
            closed = set(sim.layout.doors[door].lanes)
            for spot in sim.layout.nodes_of_kind("out_stage"):
                path = sim.manager.planner.plan("CA11_5", spot, avoid_lanes=closed, vclass="FORKLIFT")
                self.assertIsNotNone(path, f"{spot} unreachable with {door} closed")
        path = sim.manager.planner.plan("CA11_5", "SO1", avoid_lanes=set(sim.layout.doors["D-S"].lanes),
                                        vclass="FORKLIFT")
        self.assertIn("NE76_8", path)          # the detour runs down the east return lane


class LocalizationRecoveryTests(unittest.TestCase):
    def test_lost_between_nodes_needs_the_field_and_then_resumes(self):
        sim = lone_agf()
        ops = Operations(sim)
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "SE66", "test"))
        v = sim.vehicle("AGF-01")
        while sim.now < 300 and not (v.edges and 1.2 < v.progress < v._edge_len(v.edges[0]) - 1.2):
            sim.run(0.1)
        v.set_fault("LOCALIZATION_LOST")
        sim.run(2)
        res = ops.execute({"cmd": "init_position", "robot": "AGF-01"})
        self.assertFalse(res["ok"])                                  # over 1 m from any node: remote fix fails
        self.assertTrue(ops.execute({"cmd": "field_service_done", "robot": "AGF-01"})["ok"])
        while sim.now < 700 and rec.last_node != "SE66":
            sim.run(0.5)
        self.assertIsNone(rec.fatal)
        self.assertEqual(rec.last_node, "SE66")


class DispatcherTests(unittest.TestCase):
    def test_task_lifecycle_and_low_battery_charging(self):
        sim = Simulation(get("grid-night", robots=3, external=0, rate_per_h=0.0), seed=2)
        sim.run(2)
        low = sim.manager.robots["AMR-02"]
        sim.vehicle("AMR-02").battery = 20.0
        t = sim.dispatcher.add_task("PICK", "C3", None, 1)
        for _ in range(400):
            sim.run(1)
            if t.state == "DONE":
                break
        self.assertEqual(t.state, "DONE")
        self.assertTrue(low.charging or low.purpose in ("charge", "charging"))

    def test_fault_requeues_unloaded_task(self):
        sim = Simulation(get("grid-night", robots=2, external=0, rate_per_h=0.0), seed=4)
        sim.run(2)
        t = sim.dispatcher.add_task("PICK", "I7", None, 0)
        sim.run(3)
        robot = t.robot
        self.assertIsNotNone(robot)
        sim.drill_fault(robot, "DRIVE_FAULT", recoverable=False)
        sim.run(100)
        self.assertNotEqual(t.robot, robot)
        self.assertGreaterEqual(t.attempts, 1)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.sim = Simulation(get("grid-night", robots=4, rate_per_h=0.0), seed=3)
        self.sim.run(2)
        self.ops = Operations(self.sim)

    def test_read_only_rejects_and_audits(self):
        ro = Operations(self.sim, read_only=True)
        res = ro.execute({"cmd": "pause_robot", "robot": "AMR-01"}, "viewer")
        self.assertFalse(res["ok"])
        self.assertEqual(ro.audit[-1]["ok"], False)
        self.assertNotIn("AMR-01", self.sim.manager.robot_paused)

    def test_fleet_commands_need_confirmation(self):
        self.assertFalse(self.ops.execute({"cmd": "fleet_estop"})["ok"])
        self.assertFalse(self.ops.execute({"cmd": "fleet_estop", "confirm": True, "reason": " "})["ok"])   # reason
        self.assertTrue(self.ops.execute({"cmd": "fleet_estop", "confirm": True, "reason": "훈련"})["ok"])
        self.assertEqual(self.sim.manager.mode, "ESTOP")
        self.assertFalse(self.ops.execute({"cmd": "fleet_resume", "confirm": True})["ok"])
        self.ops.execute({"cmd": "fleet_estop_release", "confirm": True})
        self.assertEqual(self.sim.manager.mode, "HOLD")           # release never restarts motion
        self.sim.run(3)
        self.assertTrue(all(v.paused for v in self.sim.vehicles))
        self.ops.execute({"cmd": "fleet_resume", "confirm": True})
        self.sim.run(3)
        self.assertFalse(any(v.paused for v in self.sim.vehicles))

    def test_wms_orders_are_idempotent(self):
        seen: dict = {}
        order = {"order_id": "WMS-1", "kind": "PICK", "source": "C3", "priority": 1}
        first = self.ops.submit_wms_order(order, seen)
        again = self.ops.submit_wms_order(dict(order), seen)
        self.assertEqual(first["task"], again["task"])
        self.assertTrue(again["duplicate"])
        with self.assertRaises(ValueError):
            self.ops.submit_wms_order({**order, "source": "C4"}, seen)
        with self.assertRaises(ValueError):
            self.ops.submit_wms_order({"order_id": "bad id!", "kind": "PICK", "source": "C3"}, seen)

    def test_wms_orders_follow_site_task_kinds(self):
        sim = Simulation(get("standard"), seed=3)
        sim.run(2)
        ops = Operations(sim)
        res = ops.submit_wms_order({"order_id": "DC-1", "kind": "TOTE_PICK", "source": "K2-03"}, {})
        self.assertTrue(res["ok"])
        with self.assertRaises(ValueError):
            ops.submit_wms_order({"order_id": "DC-2", "kind": "PICK", "source": "K2-03"}, {})

    def test_teleop_takeover_round_trip(self):
        self.assertTrue(self.ops.execute({"cmd": "set_mode_manual", "robot": "AMR-01", "confirm": True})["ok"])
        self.sim.run(1)
        self.assertEqual(self.sim.manager.robots["AMR-01"].mode, "MANUAL")
        self.ops.execute({"cmd": "set_mode_auto", "robot": "AMR-01"})
        self.sim.run(1)
        self.assertEqual(self.sim.manager.robots["AMR-01"].mode, "AUTOMATIC")

    def test_shift_handover_is_audited_with_live_summary(self):
        self.assertFalse(self.ops.execute({"cmd": "handover", "note": " "})["ok"])
        self.ops.execute({"cmd": "pause_robot", "robot": "AMR-02"})
        self.assertTrue(self.ops.execute({"cmd": "handover", "note": "AMR-02 점검 대기", "by": "A조"})["ok"])
        rec = self.sim.handovers[-1]
        self.assertEqual(rec["by"], "A조")
        self.assertIn("AMR-02", rec["summary"]["paused"])

    def test_zone_evacuation_moves_robots_out(self):
        res = self.ops.execute({"cmd": "zone_evacuate", "zone": "Z-OUT", "confirm": True})
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.sim.layout.zones["Z-OUT"].locked)


if __name__ == "__main__":
    unittest.main()
