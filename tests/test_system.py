"""Whole-simulation checks: invariants, null models, dispatcher and operator commands."""
from __future__ import annotations

import random
import unittest

from fleet.autoop import AutoOperator
from fleet.ops import Operations
from fleet.scenario import get
from fleet.sim import Simulation


def sim_with_operator(name: str, seed: int = 5, **overrides) -> tuple[Simulation, Operations]:
    sim = Simulation(get(name, **overrides), seed=seed)
    ops = Operations(sim)
    sim.tick_hooks.append(AutoOperator(ops, random.Random(seed)).tick)
    return sim, ops


class InvariantTests(unittest.TestCase):
    def test_standard_shift_invariants(self):
        sim, _ = sim_with_operator("standard")
        for _ in range(300):
            sim.run(1.0)
            self.assertEqual(sim.manager.locks.check(), [])
        kpi = sim.kpi()
        self.assertGreater(kpi["done"], 0)
        amr_contacts = [c for c in sim.evaluator.contacts if c["kind"] == "amr"]
        self.assertEqual(amr_contacts, [])
        self.assertEqual(kpi["mismatch"], 0)

    def test_null_model_without_people(self):
        """With no people the people-related detectors must stay silent."""
        sim, _ = sim_with_operator("night")
        sim.run(600)
        kpi = sim.kpi()
        self.assertEqual(kpi["near_miss_truth"], 0)
        self.assertEqual(kpi["near_miss_fms"], 0)
        self.assertEqual(kpi["contacts_human"], 0)
        self.assertEqual(kpi["protective_human"], 0)
        self.assertIsNone(kpi["tracking_ratio"])

    def test_single_robot_has_no_deadlock(self):
        sim, _ = sim_with_operator("night", robots=1, external=0)
        sim.run(600)
        self.assertEqual(sim.kpi()["deadlocks"], 0)
        self.assertGreater(sim.kpi()["done"], 0)

    def test_deterministic_for_a_seed(self):
        a, _ = sim_with_operator("standard", seed=11)
        b, _ = sim_with_operator("standard", seed=11)
        a.run(120)
        b.run(120)
        self.assertEqual(a.kpi(), b.kpi())


class DispatcherTests(unittest.TestCase):
    def test_task_lifecycle_and_low_battery_charging(self):
        sim = Simulation(get("night", robots=3, external=0, rate_per_h=0.0), seed=2)
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
        sim = Simulation(get("night", robots=2, external=0, rate_per_h=0.0), seed=4)
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
        self.sim = Simulation(get("night", robots=4, rate_per_h=0.0), seed=3)
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

    def test_teleop_takeover_round_trip(self):
        self.assertTrue(self.ops.execute({"cmd": "set_mode_manual", "robot": "AMR-01", "confirm": True})["ok"])
        self.sim.run(1)
        self.assertEqual(self.sim.manager.robots["AMR-01"].mode, "MANUAL")
        self.ops.execute({"cmd": "set_mode_auto", "robot": "AMR-01"})
        self.sim.run(1)
        self.assertEqual(self.sim.manager.robots["AMR-01"].mode, "AUTOMATIC")

    def test_zone_evacuation_moves_robots_out(self):
        zone = "Z-OUT"
        res = self.ops.execute({"cmd": "zone_evacuate", "zone": zone, "confirm": True})
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.sim.layout.zones[zone].locked)


if __name__ == "__main__":
    unittest.main()
