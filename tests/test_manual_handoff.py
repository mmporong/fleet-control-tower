"""Regression checks for handing an automatic vehicle to remote manual control."""
from __future__ import annotations

import random
import unittest

from fleet.bus import Bus
from fleet.control import FleetManager
from fleet.ops import Operations
from fleet.scenario import get
from fleet.sim import Simulation
from fleet.traffic import lane_res, node_res
from fleet.vda5050 import topic


class ManualHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sim = Simulation(get("grid-night", robots=4, rate_per_h=0.0), seed=3)
        self.sim.run(2)
        self.manager = self.sim.manager
        self.rec = self.manager.robots["AMR-01"]
        self.ops = Operations(self.sim)

    def released_park_route(self) -> set[str]:
        self.assertIsNone(self.rec.task_id)
        self.assertTrue(self.manager.assign_route(self.rec, "P02", "park"))
        self.rec.base_end = min(2, len(self.rec.route) - 1)
        self.rec.state_order_id = self.rec.order_id
        self.rec.state_remaining = self.rec.base_end * 2
        for node in self.rec.route[: self.rec.base_end + 1]:
            self.manager.locks.force(self.rec.serial, node_res(node))
        for a, b in zip(self.rec.route[: self.rec.base_end], self.rec.route[1 : self.rec.base_end + 1]):
            edge = self.sim.layout.edge_between(a, b)
            self.manager.locks.force(self.rec.serial, lane_res(edge.lane))
        return set(self.manager.locks.resources_of(self.rec.serial))

    def request_manual_without_vehicle_reply(self) -> tuple[list[str], set[str]]:
        held = self.released_park_route()
        sent: list[str] = []
        self.manager.instant = lambda _serial, action_type, *_args, **_kwargs: sent.append(action_type) or action_type
        result = self.ops.execute(
            {"cmd": "set_mode_manual", "robot": self.rec.serial, "confirm": True}
        )
        self.assertTrue(result["ok"], result)
        return sent, held

    def restored_manual_handoff(self, *, pose: dict | None, factsheet: bool = True):
        self.request_manual_without_vehicle_reply()
        snapshot = self.manager.robot_snapshot(self.rec)
        restored = FleetManager(self.sim.layout, Bus(random.Random(9)))
        restored.restore({self.rec.serial: snapshot})
        if factsheet:
            restored._on_factsheet(topic("sim", self.rec.serial, "factsheet"), self.rec.factsheet)
        restored.now = 0.1
        state = {
            "operatingMode": "MANUAL",
            "orderId": "",
            "orderUpdateId": 0,
            "nodeStates": [],
            "driving": False,
            "distanceSinceLastNode": 0.0,
        }
        if pose is not None:
            state["agvPosition"] = pose
        restored._on_state(topic("sim", self.rec.serial, "state"), state)
        return restored, restored.robots[self.rec.serial], set(snapshot["held"])

    def test_taskless_park_route_is_detached_before_manual_mode_is_requested(self):
        sent, held = self.request_manual_without_vehicle_reply()

        self.assertEqual(sent, ["cancelOrder", "x_setOperatingMode"])
        self.assertEqual(self.rec.purpose, "manual_handoff")
        self.assertTrue(self.rec.route)
        self.assertIn(self.rec.serial, self.manager.cancel_pending)
        self.assertEqual(set(self.manager.locks.resources_of(self.rec.serial)), held)

    def test_manual_intent_is_persisted_before_the_first_cancel_publish(self):
        self.released_park_route()
        snapshots: list[dict] = []
        self.manager.on_order = lambda rec: snapshots.append(self.manager.robot_snapshot(rec))

        def stop_after_first_publish(_serial, action_type, *_args, **_kwargs):
            if action_type == "cancelOrder":
                raise RuntimeError("process stopped after cancel publish")
            return action_type

        self.manager.instant = stop_after_first_publish
        with self.assertRaisesRegex(RuntimeError, "process stopped"):
            self.ops.cmd_set_mode_manual({"robot": self.rec.serial})

        self.assertTrue(snapshots)
        self.assertTrue(all(snap["purpose"] == "manual_handoff" for snap in snapshots))
        persisted = snapshots[-1]
        self.assertEqual(persisted["cancel_order_id"], self.rec.state_order_id)

        restored = FleetManager(self.sim.layout, Bus(random.Random(11)))
        restored.restore({self.rec.serial: persisted})
        rec = restored.robots[self.rec.serial]
        rec.has_state = True
        rec.connection = "ONLINE"
        rec.last_seen = restored.now + 0.1
        rec.mode = "AUTOMATIC"
        rec.state_order_id = persisted["order_id"]
        rec.state_remaining = 0
        rec.driving = False
        rec.dist_since = 0.0
        rec.last_node = persisted["route"][0]
        sent: list[str] = []
        restored.instant = lambda _serial, action_type, *_args, **_kwargs: sent.append(action_type) or action_type

        restored._reconcile(rec)

        self.assertEqual(rec.purpose, "manual_handoff")
        self.assertFalse(restored.can_take_route(rec))
        self.assertEqual(sent, ["x_setOperatingMode"])

    def test_missing_vehicle_reply_keeps_released_reservations(self):
        self.rec.connection = "CONNECTION_BROKEN"
        sent, held = self.request_manual_without_vehicle_reply()

        self.assertEqual(sent, ["cancelOrder", "x_setOperatingMode"])
        self.assertEqual(self.rec.mode, "AUTOMATIC")
        self.assertEqual(set(self.manager.locks.resources_of(self.rec.serial)), held)
        self.assertIn(self.rec.serial, self.manager.cancel_pending)

    def test_mid_edge_vehicle_keeps_released_reservations_while_reply_is_pending(self):
        held = self.released_park_route()
        a, b = self.rec.route[:2]
        na, nb = self.sim.layout.nodes[a], self.sim.layout.nodes[b]
        self.rec.x, self.rec.y = (na.x + nb.x) / 2, (na.y + nb.y) / 2
        self.rec.dist_since = self.sim.layout.edge_between(a, b).length / 2
        self.rec.driving = True
        sent: list[str] = []
        self.manager.instant = lambda _serial, action_type, *_args, **_kwargs: sent.append(action_type) or action_type

        result = self.ops.execute(
            {"cmd": "set_mode_manual", "robot": self.rec.serial, "confirm": True}
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(sent, ["cancelOrder", "x_setOperatingMode"])
        self.assertEqual(set(self.manager.locks.resources_of(self.rec.serial)), held)
        self.assertIn(self.rec.serial, self.manager.cancel_pending)

    def test_cancel_confirmation_alone_does_not_end_the_manual_handoff(self):
        self.request_manual_without_vehicle_reply()
        order_id, sent_at = self.manager.cancel_pending[self.rec.serial]
        self.rec.last_seen = sent_at + 0.1
        self.rec.state_order_id = order_id
        self.rec.state_remaining = 0
        self.rec.driving = False
        self.rec.dist_since = 0.0
        self.rec.last_node = self.rec.route[0]

        self.manager._confirm_cancels()
        self.sim.dispatcher.tick(self.manager.now)
        self.manager._progress(self.rec)

        self.assertNotIn(self.rec.serial, self.manager.cancel_pending)
        self.assertEqual(self.rec.purpose, "manual_handoff")
        self.assertTrue(self.rec.route)
        self.assertFalse(self.manager.can_take_route(self.rec))
        self.assertFalse(self.sim.dispatcher._available(self.rec))

    def test_lost_manual_request_is_retried_after_cancel_confirmation(self):
        sent, _held = self.request_manual_without_vehicle_reply()
        order_id, sent_at = self.manager.cancel_pending[self.rec.serial]
        self.rec.last_seen = sent_at + 0.1
        self.rec.state_order_id = order_id
        self.rec.state_remaining = 0
        self.rec.driving = False
        self.rec.dist_since = 0.0
        self.rec.last_node = self.rec.route[0]
        self.manager._confirm_cancels()
        self.manager.now += 3.1

        self.manager._confirm_cancels()

        self.assertEqual(sent, ["cancelOrder", "x_setOperatingMode", "x_setOperatingMode"])
        self.assertEqual(self.rec.purpose, "manual_handoff")

    def test_fresh_stopped_manual_state_completes_the_handoff(self):
        self.request_manual_without_vehicle_reply()
        _order_id, sent_at = self.manager.cancel_pending[self.rec.serial]
        self.rec.last_seen = sent_at + 0.1
        self.rec.mode = "MANUAL"
        self.rec.state_order_id = ""
        self.rec.state_remaining = 0
        self.rec.driving = False
        self.rec.dist_since = 0.0
        self.rec.last_node = None

        self.manager._confirm_cancels()

        self.assertNotIn(self.rec.serial, self.manager.cancel_pending)
        self.assertEqual(self.rec.purpose, "")
        self.assertEqual(self.rec.route, [])
        self.assertFalse(self.manager.alarms.is_active(f"RESERVATION_MISMATCH:{self.rec.serial}"))

    def test_restored_manual_handoff_accepts_a_blank_manual_state(self):
        pose = {"x": self.rec.x, "y": self.rec.y, "theta": self.rec.theta,
                "mapId": "warehouse-a", "positionInitialized": True}
        restored, rec, _held = self.restored_manual_handoff(pose=pose)

        restored._reconcile(rec)

        self.assertEqual(rec.purpose, "")
        self.assertEqual(rec.route, [])
        self.assertNotIn(rec.serial, restored.cancel_pending)
        self.assertNotIn(rec.serial, restored.robot_paused)
        self.assertFalse(restored.alarms.is_active(f"RESERVATION_MISMATCH:{rec.serial}"))

    def test_restored_handoff_keeps_reservations_when_the_manual_state_has_no_pose(self):
        restored, rec, held = self.restored_manual_handoff(pose=None)

        restored._reconcile(rec)

        self.assertEqual(rec.purpose, "manual_handoff")
        self.assertEqual(set(restored.locks.resources_of(rec.serial)), held)
        self.assertTrue(rec.route)

    def test_restored_handoff_keeps_reservations_when_position_is_not_initialized(self):
        pose = {"x": self.rec.x, "y": self.rec.y, "theta": self.rec.theta,
                "mapId": "warehouse-a", "positionInitialized": False}
        restored, rec, held = self.restored_manual_handoff(pose=pose)

        restored._reconcile(rec)

        self.assertEqual(rec.purpose, "manual_handoff")
        self.assertEqual(set(restored.locks.resources_of(rec.serial)), held)
        self.assertTrue(rec.route)

    def test_restored_handoff_keeps_reservations_until_the_factsheet_arrives(self):
        pose = {"x": self.rec.x, "y": self.rec.y, "theta": self.rec.theta,
                "mapId": "warehouse-a", "positionInitialized": True}
        restored, rec, held = self.restored_manual_handoff(pose=pose, factsheet=False)

        restored._reconcile(rec)

        self.assertEqual(rec.purpose, "manual_handoff")
        self.assertEqual(set(restored.locks.resources_of(rec.serial)), held)
        self.assertTrue(rec.route)

    def test_auto_return_cannot_bypass_a_pending_handoff(self):
        self.request_manual_without_vehicle_reply()

        pending = self.ops.execute({"cmd": "set_mode_auto", "robot": self.rec.serial})
        self.ops.execute({"cmd": "resume_robot", "robot": self.rec.serial})

        self.assertFalse(pending["ok"])
        self.assertEqual(self.rec.purpose, "manual_handoff")
        self.assertFalse(self.manager.can_take_route(self.rec))

        self.rec.last_seen = self.manager.now + 0.1
        self.rec.mode = "MANUAL"
        self.rec.state_order_id = ""
        self.rec.state_remaining = 0
        self.rec.driving = False
        self.rec.dist_since = 0.0
        self.rec.last_node = None
        self.manager._confirm_cancels()

        returned = self.ops.execute({"cmd": "set_mode_auto", "robot": self.rec.serial})
        self.assertTrue(returned["ok"], returned)

    def test_confirmed_manual_handoff_can_return_to_automatic(self):
        sim = Simulation(get("grid-night", robots=4, rate_per_h=0.0), seed=4)
        sim.run(2)
        ops = Operations(sim)
        rec = sim.manager.robots["AMR-01"]

        self.assertTrue(ops.execute(
            {"cmd": "set_mode_manual", "robot": rec.serial, "confirm": True}
        )["ok"])
        sim.run(1)
        self.assertEqual((rec.mode, rec.purpose), ("MANUAL", ""))

        self.assertTrue(ops.execute({"cmd": "set_mode_auto", "robot": rec.serial})["ok"])
        sim.run(1)
        self.assertEqual(rec.mode, "AUTOMATIC")
        self.assertTrue(sim.manager.can_take_route(rec))

    def test_manual_handoff_finishes_when_the_vehicle_retains_its_last_order_id(self):
        sim = Simulation(get("grid-night", robots=4, rate_per_h=0.0), seed=5)
        sim.run(2)
        ops = Operations(sim)
        rec = sim.manager.robots["AMR-01"]
        self.assertTrue(sim.manager.assign_route(rec, "P02", "park"))
        sim.run(0.4)
        order_id = rec.order_id

        self.assertTrue(ops.execute(
            {"cmd": "set_mode_manual", "robot": rec.serial, "confirm": True}
        )["ok"])
        sim.run(1)

        self.assertEqual(rec.mode, "MANUAL")
        self.assertEqual(rec.state_order_id, order_id)
        self.assertEqual(rec.purpose, "")
        self.assertNotIn(rec.serial, sim.manager.cancel_pending)


if __name__ == "__main__":
    unittest.main()
