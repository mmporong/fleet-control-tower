"""Regression coverage for the design-peak AGF gridlock incident."""
from __future__ import annotations

import math
import unittest

from fleet.scenario import get
from fleet.sim import Simulation
from fleet.site_dc import AGF
from fleet.geometry import body_circles
from fleet.traffic import lane_res, node_res


QUIET = dict(
    amr=0,
    pickers=0,
    packers=0,
    dock_workers=0,
    walkers=0,
    dock_forklifts_in=0,
    dock_forklifts_out=0,
    rack_forklift=0,
    external=0,
    trucks_h=0.0,
    out_pallets_h=0.0,
    tote_orders_h=0.0,
    fault_mtbf_h=0.0,
    comms_mtbf_h=0.0,
)


class TurnFieldReservationTests(unittest.TestCase):
    @staticmethod
    def incident_turn():
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        blocker = manager.robots["AGF-02"]
        manager.locks.release_all(rec.serial)
        manager.locks.release_all(blocker.serial)
        rec.last_node = "qSW16_2SW22_8"
        rec.x, rec.y = manager.layout.nodes[rec.last_node].pos
        rec.route = [rec.last_node, "SW16_2", "R1-01"]
        rec.order_id = rec.state_order_id = "incident-turn"
        rec.cur_idx = rec.base_end = rec.sent_base_end = 0
        rec.last_seq = 0
        rec.last_order = None
        rec.order_pending = True
        rec.driving = False
        rec.dist_since = 0.0
        manager.locks.force(rec.serial, node_res(rec.last_node))
        manager.locks.force(blocker.serial, node_res("qSW10SW16_2"))
        return manager, rec, blocker

    def test_incident_turn_adds_only_the_forward_field_beyond_the_physical_sweep(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["qSW16_2SW22_8", "SW16_2", "R1-01"]

        swept = manager._turn_zone(rec, 1)
        field = manager._turn_approach_zone(rec, 1)

        self.assertNotIn(node_res("qSW10SW16_2"), swept)
        self.assertIn(node_res("qSW10SW16_2"), field)
        self.assertNotIn(node_res("qSW16_2SW22_8"), field)
        self.assertNotIn(node_res("SE16_2"), field)

    def test_perpendicular_agf_body_circle_blocks_the_forward_field_corner(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        blocker = manager.robots["AGF-02"]
        vehicle = sim.vehicle(rec.serial)
        blocker_vehicle = sim.vehicle(blocker.serial)
        manager.locks.release_all(rec.serial)
        manager.locks.release_all(blocker.serial)
        rec.last_node = "PA14_3"
        rec.x, rec.y = manager.layout.nodes[rec.last_node].pos
        rec.route = [rec.last_node, "BL14_3", "BL11_5"]
        rec.order_id = rec.state_order_id = "circle-corner"
        rec.cur_idx = rec.base_end = rec.sent_base_end = 0
        rec.last_seq = 0
        rec.last_order = None
        rec.order_pending = True
        rec.driving = False
        rec.dist_since = 0.0
        manager.locks.force(rec.serial, node_res(rec.last_node))
        blocker.theta = math.pi
        manager.locks.force(blocker.serial, node_res("NW16_2"))
        for a, b in zip(rec.route, rec.route[1:]):
            manager.blocks.pop(lane_res(manager.layout.edge_between(a, b).lane), None)

        # At 0.11 m before the turn (just outside the 0.10 m arrival tolerance), a perpendicular default
        # AGF parked on NW16_2 has an actual chassis circle inside the approaching AGF's stopped field.
        turn = manager.layout.nodes["BL14_3"]
        origin = (turn.x, turn.y + 0.11)
        blocker_node = manager.layout.nodes["NW16_2"]
        forward_start = rec.length / 2
        forward_end = forward_start + vehicle.spec.field_base + 0.15
        field_half_width = rec.width / 2 + 0.25
        intersects = False
        for cx, cy, radius in blocker_vehicle.spec.circles(blocker_node.x, blocker_node.y, math.pi):
            forward = origin[1] - cy
            lateral = abs(cx - origin[0])
            df = max(forward_start - forward, 0.0, forward - forward_end)
            dl = max(lateral - field_half_width, 0.0)
            intersects |= math.hypot(df, dl) <= radius
        self.assertTrue(intersects)

        manager._extend(rec)

        self.assertEqual(rec.base_end, 0)
        self.assertEqual(rec.wait.resource, node_res("NW16_2"))
        self.assertEqual(manager.locks.holder_of(node_res("NW16_2")), blocker.serial)
        self.assertIsNone(manager.locks.holder_of(node_res("BL14_3")))
        self.assertEqual(manager.locks.check(), [])

    def test_other_perpendicular_agf_corner_is_in_the_approach_zone(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["NE16_2", "NW16_2", "qNW10NW16_2"]

        self.assertIn(node_res("BL14_3"), manager._turn_approach_zone(rec, 1))

    def test_exact_body_circles_do_not_reserve_the_far_parallel_queue(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["qNE16_2NE22_8", "NE22_8", "R2-09"]

        self.assertNotIn(node_res("qNE22_8NE29_4"), manager._turn_approach_zone(rec, 1))

    def test_current_turn_uses_the_observed_pose_without_arrival_offset(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["NE22_8", "R2-09"]
        rec.cur_idx = rec.base_end = 0
        rec.order_id = rec.state_order_id = "current-turn"
        rec.driving = False
        rec.dist_since = 0.0
        rec.x, rec.y = manager.layout.nodes["NE22_8"].pos
        rec.theta = 0.0

        self.assertIn(node_res("qNE22_8NE29_4"), manager._turn_approach_zone(rec, 0))

    def test_shared_body_circle_helper_preserves_the_vehicle_model(self):
        sim = Simulation(get("standard", **{"agf": 1, **QUIET, "amr": 1}), seed=1,
                         strategy="locks+resolve")

        for vehicle in sim.vehicles:
            spec = vehicle.spec
            for heading in (0.0, math.pi / 2, math.pi):
                with self.subTest(vehicle=vehicle.serial, heading=heading):
                    radius = spec.width / 2 + 0.02
                    count = max(1, math.ceil(spec.length / spec.width))
                    offset = max(0.0, spec.length / 2 - radius)
                    distances = [0.0] if count == 1 else [
                        -offset + 2 * offset * i / (count - 1) for i in range(count)
                    ]
                    expected = [(2.0 + math.cos(heading) * d, 3.0 + math.sin(heading) * d, radius)
                                for d in distances]
                    self.assertEqual(body_circles(spec.length, spec.width, 2.0, 3.0, heading), expected)
                    self.assertEqual(spec.circles(2.0, 3.0, heading), expected)

    def test_default_agf_and_amr_fields_fit_inside_the_turn_reservation(self):
        sim = Simulation(get("standard", **{"agf": 1, **QUIET, "amr": 1}), seed=1,
                         strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        agf = next(rec for rec in manager.robots.values() if rec.vclass == AGF)
        amr = next(rec for rec in manager.robots.values() if rec.vclass != AGF)

        for rec, other in ((agf, amr), (amr, agf)):
            with self.subTest(vehicle=rec.serial):
                field_base = sim.vehicle(rec.serial).spec.field_base
                stopped_field_reach = rec.length / 2 + field_base + 0.15 + other.length / 2
                self.assertGreaterEqual(manager._turn_field_reach(rec), stopped_field_reach)

    def test_turn_at_route_start_uses_heading_across_the_pi_boundary(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["SW16_2", "R1-01"]
        rec.x, rec.y = manager.layout.nodes["SW16_2"].pos
        rec.cur_idx = rec.base_end = 0
        rec.order_id = rec.state_order_id = "route-start-turn"
        rec.driving = False
        rec.dist_since = 0.0

        for heading in (3.141592653589793, -3.141592653589793):
            with self.subTest(heading=heading):
                rec.theta = heading
                self.assertIn(node_res("qSW10SW16_2"), manager._turn_approach_zone(rec, 0))

    def test_distant_turn_without_extra_field_keeps_the_original_release(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        manager.locks.release_all(rec.serial)
        rec.last_node = "qNE10NE16_2"
        rec.x, rec.y = manager.layout.nodes[rec.last_node].pos
        rec.route = [rec.last_node, "NE16_2", "qNE16_2NE22_8", "NE22_8", "R2-09"]
        rec.order_id = rec.state_order_id = "future-turn"
        rec.cur_idx = rec.base_end = rec.sent_base_end = 0
        rec.last_seq = 0
        rec.last_order = None
        rec.order_pending = True
        rec.driving = False
        rec.dist_since = 0.0
        manager.locks.force(rec.serial, node_res(rec.last_node))
        for a, b in zip(rec.route, rec.route[1:]):
            manager.blocks.pop(lane_res(manager.layout.edge_between(a, b).lane), None)

        manager._extend(rec)

        self.assertEqual(rec.base_end, 4)
        self.assertIsNone(manager.locks.holder_of(node_res("qNE22_8NE29_4")))
        self.assertNotIn(node_res("qNE22_8NE29_4"), rec.rot_zone.get(3, set()))

    def test_next_atomic_no_stop_chain_is_released_with_its_forward_guard(self):
        manager, rec, blocker = self.incident_turn()
        manager.locks.release(blocker.serial, [node_res("qSW10SW16_2")])

        manager._extend(rec)

        self.assertEqual(rec.base_end, 2)
        self.assertEqual(manager.locks.holder_of(node_res("qSW10SW16_2")), rec.serial)

    def test_stub_gate_keeps_the_original_physical_turn_reach(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        other = manager.robots["AGF-02"]
        expected = (rec.length ** 2 + rec.width ** 2) ** 0.5 / 2 + other.length / 2 + 0.2

        self.assertAlmostEqual(manager._turn_reach(rec), expected)

    def test_held_field_node_keeps_the_whole_turn_unreleased_without_taking_locks(self):
        manager, rec, blocker = self.incident_turn()
        current = node_res(rec.last_node)
        field_node = node_res("qSW10SW16_2")

        manager._extend(rec)

        self.assertEqual(rec.base_end, 0)
        self.assertEqual(rec.wait.resource, field_node)
        self.assertEqual(manager.locks.holder_of(field_node), blocker.serial)
        self.assertEqual(manager.locks.holder_of(current), rec.serial)
        self.assertIsNone(manager.locks.holder_of(node_res("SW16_2")))
        self.assertEqual(manager.locks.check(), [])

    def test_turn_proceeds_after_the_field_node_is_released_then_drops_its_extra_lock(self):
        manager, rec, blocker = self.incident_turn()
        field_node = node_res("qSW10SW16_2")
        blocker_occupied = node_res(blocker.last_node)
        manager.locks.force(blocker.serial, blocker_occupied)
        manager._extend(rec)
        manager.locks.release(blocker.serial, [field_node])

        manager._extend(rec)

        self.assertEqual(rec.base_end, 2)
        self.assertEqual(manager.locks.holder_of(field_node), rec.serial)
        self.assertEqual(manager.locks.holder_of(blocker_occupied), blocker.serial)
        rec.last_node = "R1-01"
        rec.x, rec.y = manager.layout.nodes[rec.last_node].pos
        rec.last_seq = 4
        rec.state_order_id = rec.order_id
        rec.driving = False
        rec.dist_since = 0.0
        manager._progress(rec)
        self.assertIsNone(manager.locks.holder_of(field_node))
        self.assertEqual(manager.locks.holder_of(blocker_occupied), blocker.serial)
        self.assertEqual(manager.locks.check(), [])


if __name__ == "__main__":
    unittest.main()
