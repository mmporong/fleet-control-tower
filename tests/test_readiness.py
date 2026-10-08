"""Regression tests for the field-readiness review: safety resets, task exceptions, standard-only vehicles,
tolerant message handling, comms staging, multi-order picking, cost-based assignment and speed rules."""
from __future__ import annotations

import doctest
import unittest

from fleet import clock as clock_mod
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
    tests.addTests(doctest.DocTestSuite(clock_mod))
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
