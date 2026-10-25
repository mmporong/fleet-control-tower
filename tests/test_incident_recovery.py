"""Regression coverage for the design-peak AGF gridlock incident."""
from __future__ import annotations

import unittest

from fleet.scenario import get
from fleet.sim import Simulation
from fleet.site_dc import AGF
from fleet.traffic import node_res


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

    def test_incident_turn_reserves_the_queue_node_that_the_vehicle_field_sees(self):
        sim = Simulation(get("standard", **{"agf": 2, **QUIET}), seed=1, strategy="locks+resolve")
        sim.run(2.0)
        manager = sim.manager
        rec = manager.robots["AGF-01"]
        rec.route = ["qSW16_2SW22_8", "SW16_2", "R1-01"]

        reserved = manager._turn_zone(rec, 1)

        self.assertIn(node_res("qSW10SW16_2"), reserved)

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
                self.assertGreaterEqual(manager._turn_reach(rec), stopped_field_reach)

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
