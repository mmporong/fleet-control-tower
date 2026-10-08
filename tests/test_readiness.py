"""Regression tests for the field-readiness review: safety resets, task exceptions, standard-only vehicles,
tolerant message handling, comms staging, multi-order picking, cost-based assignment and speed rules."""
from __future__ import annotations

import doctest
import unittest

from fleet import auth as auth_mod
from fleet import bench as bench_mod
from fleet import clock as clock_mod
from fleet import control as control_mod
from fleet import dispatcher as dispatcher_mod
from fleet import store as store_mod
from fleet.dispatcher import Task
from fleet.layout import warehouse_layout
from fleet.ops import Operations
from fleet.process_dc import DcProcess, location_id
from fleet.scenario import get
from fleet.sim import Simulation
from fleet.site_dc import AGF, dc_layout
from fleet.vda5050 import topic

from tests.helpers import MiniWorld


def load_tests(loader, tests, ignore):
    for mod in (clock_mod, auth_mod, dispatcher_mod, store_mod, control_mod, bench_mod):
        tests.addTests(doctest.DocTestSuite(mod))
    return tests


QUIET = dict(amr=0, pickers=0, packers=0, dock_workers=0, walkers=0, dock_forklifts_in=0, dock_forklifts_out=0,
             rack_forklift=0, external=0, trucks_h=0.0, out_pallets_h=0.0, tote_orders_h=0.0, fault_mtbf_h=0.0,
             comms_mtbf_h=0.0, handling_fail_p=0.0, inventory_error_p=0.0)


def quiet_dc(seed: int = 3, **overrides) -> Simulation:
    sim = Simulation(get("standard", **{"agf": 1, **QUIET, **overrides}), seed=seed)
    sim.run(2)
    return sim


def run_until(sim: Simulation, cond, limit: float) -> bool:
    end = sim.now + limit
    while sim.now < end:
        sim.step()
        if cond():
            return True
    return False


class SafetyResetTests(unittest.TestCase):
    def test_safety_faults_are_not_reset_remotely(self):
        sim = quiet_dc()
        ops = Operations(sim)
        v = sim.vehicles[0]
        for fault in ("SENSOR_FAULT", "BUMPER"):
            v.set_fault(fault)
            sim.run(2)
            res = ops.execute({"cmd": "reset_fault", "robot": v.serial})
            self.assertFalse(res["ok"])
            self.assertIn("원격 리셋 불가", res["msg"])
            sim.manager.instant(v.serial, "x_resetFault")          # even a raw command is refused by the vehicle
            sim.run(2)
            self.assertEqual(v.fault, fault)
            self.assertTrue(ops.execute({"cmd": "field_service_done", "robot": v.serial})["ok"])
            sim.run(2)
            self.assertIsNone(v.fault)

    def test_non_safety_fault_can_be_reset_remotely(self):
        sim = quiet_dc()
        ops = Operations(sim)
        v = sim.vehicles[0]
        v.set_fault("DRIVE_FAULT", recoverable=True)
        sim.run(2)
        self.assertTrue(ops.execute({"cmd": "reset_fault", "robot": v.serial})["ok"])
        sim.run(2)
        self.assertIsNone(v.fault)


class TaskExceptionTests(unittest.TestCase):
    def _putaway(self, sim: Simulation) -> Task:
        proc: DcProcess = sim.process
        spot = "SI1"
        pid = proc._new_pallet(("in_stage", spot), sim.now)
        proc.in_stage[spot].append(pid)
        task = proc._make_putaway(spot, pid, sim.now)
        sim.dispatcher.submit(task)
        return task

    def test_occupied_slot_redirects_the_putaway_and_books_the_location(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        first = proc._free_slot()
        proc.phys[first] = "UNRECORDED"                # the WMS believes it is empty
        task = self._putaway(sim)
        self.assertEqual(task.meta["slot"], first)
        self.assertTrue(run_until(sim, lambda: task.state == "DONE", 900), task.note)
        self.assertEqual(proc.slots[first], "UNKNOWN")
        new = task.meta["slot"]
        self.assertNotEqual(new, first)
        self.assertEqual(proc.slots[new], task.meta["pallet"])
        codes = [e.get("code") for e in sim.dispatcher.outbox if e["type"] == "EXCEPTION"]
        self.assertIn("SLOT_OCCUPIED", codes)
        self.assertEqual(proc.metrics["redirects"], 1)

    def test_missing_pallet_redirects_the_retrieval_to_another_pallet(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        slot = proc._oldest_full_slot()
        proc.phys[slot] = None                         # the record says full, the location is empty
        task = proc._make_pallet_out(0)
        self.assertEqual(task.meta["slot"], slot)
        sim.dispatcher.submit(task)
        self.assertTrue(run_until(sim, lambda: task.state == "DONE", 900), task.note)
        self.assertIsNone(proc.slots[slot])
        self.assertNotEqual(task.meta["slot"], slot)
        self.assertIn("PALLET_NOT_FOUND", [e.get("code") for e in sim.dispatcher.outbox])

    def test_pocket_not_recognised_is_retried_once(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        fails = iter([True, False])
        real = proc.physical_check
        proc.physical_check = None
        sim.vehicles[0].station_check = lambda s, a: "팔레트 포켓 인식 실패" if a.action_type == "pick" and \
            next(fails, False) else real(s, a)
        task = self._putaway(sim)
        self.assertTrue(run_until(sim, lambda: task.state == "DONE", 900), task.note)
        self.assertEqual(proc.metrics["retries"], 1)
        self.assertIn("PICK_RETRY", [e.get("code") for e in sim.dispatcher.outbox])

    def test_outbox_reports_the_task_lifecycle_in_order(self):
        sim = quiet_dc()
        task = self._putaway(sim)
        self.assertTrue(run_until(sim, lambda: task.state == "DONE", 900))
        kinds = [e["type"] for e in sim.dispatcher.events_after(0) if e["task"] == task.id]
        self.assertEqual(kinds, ["CREATED", "ASSIGNED", "STOP_DONE", "STOP_DONE", "COMPLETED"])
        seqs = [e["seq"] for e in sim.dispatcher.outbox]
        self.assertEqual(seqs, sorted(seqs))
        last = seqs[-1]
        self.assertEqual(sim.dispatcher.events_after(last), [])


class StandardOnlyVehicleTests(unittest.TestCase):
    def setUp(self):
        self.w = MiniWorld({"AMR-01": "B5"})
        self.v = self.w.vehicles["AMR-01"]
        self.v.extensions = False                   # a second vendor: only the standard actions
        self.v._factsheet_due = True
        self.w.run(1.0)
        self.rec = self.w.manager.robots["AMR-01"]

    def test_factsheet_declares_no_extensions_and_state_has_no_x_fields(self):
        acts = {a["actionType"] for a in self.rec.factsheet["protocolFeatures"]["agvActions"]}
        self.assertFalse(any(a.startswith("x_") for a in acts))
        state = self.v.state_msg(self.w.now)
        self.assertFalse(any(k.startswith("x_") for k in state))
        self.assertIn("구역 감속 → 경로 maxSpeed 상시 적용", self.w.manager.capability_gaps(self.rec))

    def test_fleet_stop_falls_back_to_start_pause(self):
        self.w.manager.mode = "ESTOP"
        self.w.run(1.5)
        self.assertTrue(self.v.paused)
        self.assertFalse(self.v.estop)
        self.w.manager.mode = "RUNNING"
        self.w.run(1.5)
        self.assertFalse(self.v.paused)

    def test_people_zone_limits_travel_in_edge_max_speed(self):
        m = self.w.manager
        zone = next(z for z in self.w.layout.zones.values() if z.human_speed is not None)
        node = next(n for n, zs in self.w.layout.node_zones.items() if zone.id in zs)
        edge = next(e for e in self.w.layout.in_edges[node])
        self.assertLessEqual(m._edge_speed(self.rec, edge), zone.human_speed)
        other = MiniWorld({"AMR-02": "B5"})
        other.run(1.0)
        rec2 = other.manager.robots["AMR-02"]
        self.assertGreater(other.manager._edge_speed(rec2, edge), zone.human_speed)   # it obeys x_fleetSpeedLimit


class InboundToleranceTests(unittest.TestCase):
    def test_state_without_optional_fields_is_accepted(self):
        w = MiniWorld({"AMR-01": "B5"})
        w.run(1.0)
        msg = w.vehicles["AMR-01"].state_msg(w.now)
        for k in ("velocity", "loads", "distanceSinceLastNode", "paused", "x_safetyField", "x_detections"):
            msg.pop(k, None)
        msg["safetyState"] = {"eStop": "NONE", "fieldViolation": False}
        w.manager._on_state(topic("sim", "AMR-01", "state"), msg)
        rec = w.manager.robots["AMR-01"]
        self.assertEqual(rec.v, 0.0)
        self.assertFalse(rec.loaded)
        self.assertEqual(rec.dist_since, 0.0)

    def test_malformed_message_is_dropped_and_counted(self):
        w = MiniWorld({"AMR-01": "B5"})
        w.manager.validator = lambda kind, msg: [] if "headerId" in msg else ["missing headerId"]
        w.manager._on_state(topic("sim", "AMR-01", "state"), {"garbage": True})
        self.assertEqual(w.manager.counters["invalid_msgs"], 1)
        self.assertTrue(any(e["type"] == "invalid_msg" for e in w.manager.events))

    def test_protective_stop_is_read_from_the_standard_flag(self):
        w = MiniWorld({"AMR-01": "B5"})
        w.run(1.0)
        msg = w.vehicles["AMR-01"].state_msg(w.now)
        msg.pop("x_safetyField", None)
        msg["safetyState"]["fieldViolation"] = True
        w.manager._on_state(topic("sim", "AMR-01", "state"), msg)
        self.assertEqual(w.manager.robots["AMR-01"].safety_field, "PROTECTIVE")


class CommsStagingTests(unittest.TestCase):
    def test_short_silence_is_an_alert_and_a_long_one_an_alarm(self):
        w = MiniWorld({"AMR-01": "B5"})
        w.run(1.0)
        a = w.manager.alarms
        w.bus.link("AMR-01").down_until = w.now + 8.0
        w.run(6.0)
        self.assertTrue(a.is_active("COMMS_DEGRADED:AMR-01"))
        self.assertFalse(a.is_active("COMMS_LOST:AMR-01"))
        before = a.total_activations
        w.run(4.0)
        self.assertFalse(a.is_active("COMMS_DEGRADED:AMR-01"))       # back after 8 s: no alarm was raised
        self.assertEqual(a.total_activations, before)
        w.bus.link("AMR-01").down_until = w.now + 40.0
        w.run(35.0)
        self.assertTrue(a.is_active("COMMS_LOST:AMR-01"))


class MultiOrderPickingTests(unittest.TestCase):
    def test_orders_are_batched_and_counted_per_order(self):
        sim = Simulation(get("standard", fault_mtbf_h=0.0, comms_mtbf_h=0.0, trucks_h=0.0, out_pallets_h=0.0,
                             tote_orders_h=120.0), seed=4)
        sim.run(900)
        proc: DcProcess = sim.process
        totes = [t for t in list(sim.dispatcher.tasks.values()) + list(sim.dispatcher.finished)
                 if t.kind == "TOTE_PICK"]
        self.assertTrue(totes)
        self.assertTrue(any(len(t.meta["orders"]) == 3 for t in totes))
        done = [t for t in sim.dispatcher.finished if t.kind == "TOTE_PICK" and t.state == "DONE"]
        self.assertGreater(proc.metrics["tote_done"], 0)
        self.assertEqual(proc.metrics["tote_done"], sum(len(t.meta["orders"]) for t in done))
        # units are counted per finished pick, so batches still on the way add to them
        self.assertGreaterEqual(proc.metrics["units"], sum(int(s.params["items"]) for t in done for s in t.stops
                                                           if s.action == "x_pickItems"))

    def test_single_order_mode_is_kept_for_comparison(self):
        sim = Simulation(get("standard", fault_mtbf_h=0.0, comms_mtbf_h=0.0, trucks_h=0.0, out_pallets_h=0.0,
                             tote_orders_h=120.0, tote_batch=1), seed=4)
        sim.run(300)
        totes = [t for t in sim.dispatcher.tasks.values() if t.kind == "TOTE_PICK"]
        self.assertTrue(totes)
        self.assertTrue(all(len(t.meta["orders"]) == 1 for t in totes))


class AssignmentPolicyTests(unittest.TestCase):
    def _two_tasks(self, policy: str):
        sim = quiet_dc(agf=1, dispatch=policy)
        proc: DcProcess = sim.process
        rec = sim.manager.robots["AGF-01"]
        here = rec.last_node
        near = min(proc.rack_nodes, key=lambda n: sim.manager.planner.travel_time(here, n, AGF))
        far = max(proc.rack_nodes, key=lambda n: sim.manager.planner.travel_time(here, n, AGF))
        slot_far = next(s for s in proc.slots if s[0] == far and proc.slots[s] is not None)
        slot_near = next(s for s in proc.slots if s[0] == near and proc.slots[s] is not None)
        old = proc._make_pallet_out(0, slot=slot_far, spot="SO1")
        old.created = sim.now - 60.0                     # waited a minute
        young = proc._make_pallet_out(0, slot=slot_near, spot="SO2")
        sim.dispatcher.submit(old)
        sim.dispatcher.submit(young)
        sim.run(1.0)
        return old, young

    def test_cost_policy_takes_the_nearby_task_first(self):
        old, young = self._two_tasks("cost")
        self.assertEqual(young.state, "ACTIVE")
        self.assertEqual(old.state, "QUEUED")

    def test_fifo_policy_takes_the_oldest_task_first(self):
        old, young = self._two_tasks("fifo")
        self.assertEqual(old.state, "ACTIVE")
        self.assertEqual(young.state, "QUEUED")


class SpeedRuleTests(unittest.TestCase):
    def test_blind_intersections_follow_sight_lines(self):
        dc = dc_layout()
        self.assertIn("NW47", dc.blind)                  # the fire wall hides the crossing traffic
        self.assertNotIn("SW16_2", dc.blind)             # racks are set back 2.4 m from the main aisle
        grid = warehouse_layout()
        self.assertIn("B5", grid.blind)                  # racks close to the cross row

    def test_vehicle_slows_more_at_a_blind_intersection(self):
        w = MiniWorld({"AMR-01": "B4"})
        v = w.vehicles["AMR-01"]
        self.assertIn("B5", w.layout.blind)
        w.run(1.0)
        w.send("AMR-01", "B6")
        speeds = []
        for _ in range(200):
            w.step()
            if v.edges and v.edges[0]["endNodeId"] == "B5" and \
                    v._edge_len(v.edges[0]) - v.progress < v.spec.intersection_radius:
                speeds.append(v.v)
        self.assertTrue(speeds)
        self.assertLessEqual(max(speeds), v.spec.blind_speed + 1e-6)

    def test_order_states_the_station_heading_and_zone_speed(self):
        sim = quiet_dc()
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "SO1", "test"))
        sim.run(1.0)
        order = rec.last_order
        last = order["nodes"][-1] if order["nodes"][-1]["nodeId"] == "SO1" else None
        if last is not None:
            self.assertIn("theta", last["nodePosition"])
        speeds = {e["edgeId"]: e["maxSpeed"] for e in order["edges"]}
        for eid, sp in speeds.items():
            e = sim.layout.edges[eid]
            for zid in sim.manager._edge_zones(e):
                z = sim.layout.zones[zid]
                if z.static_speed is not None:
                    self.assertLessEqual(sp, z.static_speed)


class RackModelTests(unittest.TestCase):
    def test_rack_has_realistic_capacity_and_location_names(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        self.assertEqual(len(proc.slots), 45 * 2 * 4 * 2)
        slot = next(iter(proc.slots))
        self.assertEqual(proc.loc_index[location_id(slot)], slot)
        self.assertRegex(location_id(slot), r"^R\d-\d\d-[LR]-[1-4]-[12]$")
        self.assertLessEqual(max(s[2] for s in proc.slots) * 1.4, 4.5)     # top beam within the AGF lift

    def test_putaway_fills_low_levels_near_inbound_first(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        s = proc._free_slot()
        self.assertEqual(s[2], min(k[2] for k, v in proc.slots.items() if v is None))

    def test_retrieval_is_fifo(self):
        sim = quiet_dc()
        proc: DcProcess = sim.process
        s = proc._oldest_full_slot()
        oldest = min(proc.pallets[p]["stored"] for p in proc.slots.values() if p not in (None, "UNKNOWN"))
        self.assertEqual(proc.pallets[proc.slots[s]]["stored"], oldest)


if __name__ == "__main__":
    unittest.main()


class OfficialSchemaTests(unittest.TestCase):
    def test_every_published_message_passes_the_official_schema(self):
        from fleet.schema import validate_message
        from fleet.vda5050 import split_topic
        sim = Simulation(get("standard", fault_mtbf_h=1.0, comms_mtbf_h=1.0), seed=2)
        problems: list[str] = []
        seen: set[str] = set()
        orig = sim.bus.publish

        def checked(t, payload, now):
            parts = split_topic(t)
            if parts:
                seen.add(parts[2])
                problems.extend(f"{parts[2]}: {p}" for p in validate_message(parts[2], payload)[:1])
            return orig(t, payload, now)

        sim.bus.publish = checked
        for v in sim.vehicles:
            problems.extend(validate_message("connection", v.connection_msg(0.0)))
        sim.run(240)
        self.assertEqual(problems[:5], [])
        self.assertTrue({"state", "visualization", "order", "instantActions", "factsheet"} <= seen)

    def test_spec_errata_are_applied_on_purpose(self):
        from fleet.schema import load_vda5050_schemas, validate
        msg = {"headerId": 1, "timestamp": "2026-10-01T00:00:00.000Z", "version": "2.0.0", "manufacturer": "sim",
               "serialNumber": "AMR-01", "actions": [{"actionType": "startPause", "actionId": "a1",
                                                      "blockingType": "HARD"}]}
        self.assertEqual(validate(load_vda5050_schemas()["instantActions"], msg), [])
        raw = load_vda5050_schemas(errata=False)["instantActions"]
        self.assertTrue(any("actionName" in p for p in validate(raw, msg)))   # the published file's quirk


class RealismTests(unittest.TestCase):
    def test_an_inattentive_person_can_walk_into_a_moving_vehicle(self):
        from fleet.agents import Human, Walker
        layout = warehouse_layout()
        for inattentive, expect_contact in ((False, False), (True, True)):
            h = Human("P1", "walker", 21.0, 2.2, random_rng(1))
            h.inattentive = inattentive
            h.walker = Walker([(21.0, 9.0)])                  # straight across the robot's lane
            robot = ("AMR-01", 21.0, 3.6, 0.45, 0.8)          # a robot driving past right in the person's way
            touched = False
            for k in range(30):
                h.step(0.1, k * 0.1, layout, [robot], [])
                touched = touched or ((h.x - robot[1]) ** 2 + (h.y - robot[2]) ** 2) ** 0.5 < 0.45 + h.radius
            self.assertEqual(touched, expect_contact, inattentive)

    def test_contacts_are_all_counted_and_a_struck_vehicle_reports_its_bumper(self):
        sim = quiet_dc(rack_forklift=0)
        v = sim.vehicles[0]
        from fleet.agents import Human
        h = Human("X1", "walker", v.x + 0.6, v.y, random_rng(2))
        h.v = 1.0
        sim.humans.append(h)
        found = sim.evaluator.check(sim.now, sim.vehicles, sim.humans, [], [])
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["moving"])
        self.assertTrue(found[0]["other_moving"])
        sim.humans.remove(h)
        k = sim.kpi()
        self.assertEqual((k["contacts"], k["contacts_struck"], k["contacts_moving"]), (1, 1, 0))

    def test_robot_leaves_the_swept_circle_of_a_stopped_truck(self):
        from fleet.perception import Detection
        w = MiniWorld({"AMR-01": "B5"})
        w.run(1.0)
        rec = w.manager.robots["AMR-01"]
        n = w.layout.nodes["B5"]
        w.detections = [Detection("forklift", n.x + 1.5, n.y, "infra")]   # a truck stopped right beside it
        moved = w.run_until(lambda: w.vehicles["AMR-01"].last_node_id != "B5", 20.0)
        self.assertTrue(moved, [e["msg"] for e in w.manager.events])
        self.assertTrue(any(e["type"] == "yield" and "선회" in e["msg"] for e in w.manager.events))
        self.assertIsNone(rec.wait)

    def test_a_single_false_detection_does_not_block(self):
        from fleet.perception import Detection, Tracker
        tr = Tracker()
        tr.update([Detection("human", 10.0, 10.0, "infra")], 0.0)
        self.assertEqual(tr.predicted_discs(0.0, {"human": 2.0}), [])
        tr.update([Detection("human", 10.05, 10.0, "infra")], 0.5)
        self.assertTrue(tr.predicted_discs(0.5, {"human": 2.0}))

    def test_infrastructure_levels(self):
        none = Simulation(get("standard", infra="none", fault_mtbf_h=0.0, comms_mtbf_h=0.0), seed=1)
        self.assertEqual(none.layout.coverage, {})
        none.run(30)
        self.assertIsNone(none.manager.infra_last)                       # no camera feed exists
        self.assertTrue(none.manager.tracker.tracks)                     # the vehicles' own sensors still report
        full = Simulation(get("standard", infra="full", fault_mtbf_h=0.0, comms_mtbf_h=0.0), seed=1)
        self.assertTrue(all(full.layout.covered(h.pos) for h in full.humans))

    def test_measurement_window_and_time_averaged_utilization(self):
        sim = Simulation(get("standard", fault_mtbf_h=0.0, comms_mtbf_h=0.0), seed=3)
        sim.run(300)
        sim.reset_measurement()
        self.assertEqual(sim.kpi()["done"], 0)
        sim.run(600)
        k = sim.kpi()
        self.assertAlmostEqual(k["sim_hours"], 600 / 3600, places=3)
        self.assertTrue(0.0 <= k["utilization"] <= 1.0)
        prod = k["productivity"]
        self.assertIn("moves_per_vehicle_h", prod["FORKLIFT"])
        self.assertAlmostEqual(sum(prod["FORKLIFT"]["time_share"].values()), 1.0, places=2)
        self.assertIn("uph", prod["pickers"])


def random_rng(seed: int):
    import random
    return random.Random(seed)


class CollaborativeZoneTests(unittest.TestCase):
    def blocked_by_people(self, sim: Simulation, points: list[tuple[float, float]]) -> set[str]:
        from fleet.perception import Detection, Tracker
        m = sim.manager
        m.tracker = Tracker()
        for dt in (-0.5, 0.0):                             # two sightings confirm a track
            m.tracker.update([Detection("human", x, y, "infra") for x, y in points], m.now + dt)
        m._update_blocks()
        return {r for r, (kind, _ref) in m.blocks.items() if kind == "human"}

    def test_a_picker_holds_only_the_stop_they_stand_on(self):
        sim = quiet_dc()
        k1, k2 = sim.layout.nodes["K2-01"], sim.layout.nodes["K2-02"]
        # between two pick faces (1.5 m from each): robots may pass the picker at the aisle's speed limit
        self.assertEqual(self.blocked_by_people(sim, [(k1.x, (k1.y + k2.y) / 2)]), set())
        # on a pick face: that stop is the picker's
        self.assertEqual(self.blocked_by_people(sim, [(k2.x, k2.y + 0.2)]), {"N:K2-02"})

    def test_outside_collaborative_zones_a_person_blocks_the_lane_around_them(self):
        sim = quiet_dc()
        r = sim.layout.nodes["R1-03"]                      # rack aisle: not a collaborative zone
        held = self.blocked_by_people(sim, [(r.x, r.y + 1.5)])
        self.assertTrue(any(res.startswith("L:") for res in held), held)


class FacilityAndTruckSignalTests(unittest.TestCase):
    def test_fire_alarm_closes_doors_and_stops_releasing(self):
        sim = quiet_dc()
        ops = Operations(sim)
        rec = sim.manager.robots["AGF-01"]
        self.assertTrue(sim.manager.assign_route(rec, "SO1", "test"))      # a trip through a fire door
        sim.run(20)
        sim.drill_fire(True)
        sim.run(1.0)
        self.assertEqual(sim.manager.mode, "FIRE")
        self.assertTrue(sim.manager.alarms.is_active("FIRE_ALARM:facility"))
        base_end = rec.base_end
        sim.run(60)
        self.assertEqual(rec.base_end, base_end)                  # nothing new was released
        v = sim.vehicles[0]
        self.assertFalse(v.driving())
        self.assertNotIn(v.last_node_id, sim.layout.no_stop)      # it did not stop in a door threshold
        self.assertTrue(all(d.progress == 0.0 for d in sim.doors.values()))   # fire doors shut
        self.assertFalse(ops.execute({"cmd": "fleet_resume", "confirm": True})["ok"])
        sim.drill_fire(False)
        sim.run(1.0)
        self.assertEqual(sim.manager.mode, "HOLD")                # all clear: resume after a site check
        self.assertTrue(ops.execute({"cmd": "fleet_resume", "confirm": True})["ok"])

    def test_a_truck_waiting_at_an_aisle_mouth_gets_the_aisle(self):
        from fleet.perception import Detection
        sim = quiet_dc()
        m = sim.manager
        mouth = sim.layout.nodes["SW16_2"]
        for k in range(30):                                       # the truck stands at the R1 mouth for 6 s
            m.now = sim.now + 0.2 * k
            m.ingest_detections([Detection("forklift", mouth.x - 1.0, mouth.y, "infra")])
            m._update_blocks()
        r1 = [n for n, zs in sim.layout.node_zones.items() if "Z-R1" in zs]
        self.assertTrue(r1)
        # an aisle request (kept apart from blocks: robots already in the aisle may still leave it)
        self.assertTrue(all(f"N:{n}" in m.aisle_requests for n in r1))

    def test_turn_zone_ahead_of_a_moving_truck_is_kept_clear(self):
        from fleet.perception import Detection
        sim = quiet_dc()
        m = sim.manager
        node = sim.layout.nodes["SW22_8"]
        t0 = sim.now
        for k in range(8):                                        # a truck driving west at 1.5 m/s toward SW22_8
            m.now = t0 + 0.5 * k
            m.ingest_detections([Detection("forklift", node.x + 6.0 - 0.75 * k, node.y, "infra")])
        m._update_blocks()
        near = [n.id for n in sim.layout.nodes.values()
                if ((n.x - node.x) ** 2 + (n.y - node.y) ** 2) ** 0.5 <= 2.8]
        self.assertTrue(near)
        self.assertTrue(all(f"N:{n}" in m.blocks for n in near), [n for n in near if f"N:{n}" not in m.blocks])
