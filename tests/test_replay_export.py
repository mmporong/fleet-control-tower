"""Recorded comparisons preserve benchmark conditions and observed wait evidence."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import unittest
from unittest.mock import patch

from fleet.bench import run_one
from fleet.scenario import get
from fleet.sim import Simulation
from fleet.traffic import WaitInfo
from tools.export_3d_replay import export_replay, replay_frame


class ReplayExportTests(unittest.TestCase):
    def test_job_summary_matches_benchmark_outside_the_clip(self):
        job = {"scenario": "standard", "seed": 3, "strategy": "locks+resolve", "operator": True,
               "warmup": 10, "duration": 60, "manager": {}, "vehicle": {},
               "scenario_overrides": {"agf": 2, "amr": 2, "fault_mtbf_h": 0.003}}
        original = deepcopy(job)
        expected = run_one(job)["kpi"]
        replay = export_replay(15, 2, 0.5, 4, 99, job=job)
        summary = replay["measurement"]["summary"]
        for key, benchmark_key in (("putawayPerHour", "site_putaway_h"), ("shippedPerHour", "site_shipped_h"),
                                   ("totePerHour", "site_tote_h"), ("queued", "queued"),
                                   ("contacts", "contacts"), ("movingContacts", "contacts_moving"),
                                   ("mismatch", "mismatch"), ("lockProblems", "lock_problems")):
            self.assertEqual(summary[key], expected[benchmark_key], key)
        self.assertEqual(replay["measurement"]["startTime"], 10)
        self.assertEqual(replay["measurement"]["endTime"], 70)
        self.assertEqual(summary["durationSeconds"], 60)
        self.assertEqual([frame["t"] for frame in replay["frames"]], [15, 15.5, 16, 16.5, 17])
        self.assertEqual(replay["source"]["seed"], 3)
        self.assertEqual(replay["source"]["job"], original)
        self.assertEqual(job, original)
        self.assertIn("fleet/control.py", replay["source"]["files"])
        json.dumps(replay, allow_nan=False)

    def test_reference_keeps_operator_disabled_and_original_frames(self):
        replay = export_replay(0.5, 1, 0.5, 8, 7)
        sim = Simulation(get("standard"), seed=7, strategy="locks+resolve")
        sim.run(0.5)
        self.assertEqual(replay["frames"][0], replay_frame(sim))
        self.assertFalse(replay["source"]["job"]["operator"])

    def test_wait_evidence_and_events_are_copied_without_changing_the_simulator(self):
        sim = Simulation(get("grid-night", robots=1, external=0), seed=1)
        sim.run(1)
        rec = next(iter(sim.manager.robots.values()))
        rec.wait = WaitInfo(sim.manager.now - 0.8, "robot", "N:test-resource", "blocked-by")
        rec.reason = "예약 해제 대기"
        sim.manager.event("deadlock", rec.serial, "대기 사슬 확인", "warning")
        before = deepcopy(list(sim.manager.events))
        frame = replay_frame(sim)
        robot = next(r for r in frame["robots"] if r["id"] == rec.serial)
        self.assertEqual(robot["wait_resource"], "N:test-resource")
        self.assertEqual(robot["wait_holder"], "blocked-by")
        self.assertEqual(robot["why"], "예약 해제 대기")
        self.assertEqual(robot["wait_s"], 0.8)
        self.assertEqual(frame["events"][-1]["kind"], "deadlock")
        self.assertEqual(list(sim.manager.events), before)

        seen: set[tuple] = set()
        self.assertEqual(replay_frame(sim, seen)["events"], frame["events"])
        self.assertEqual(replay_frame(sim, seen)["events"], [])
        sim.manager.event("resumed", rec.serial, "운행 재개")
        self.assertEqual([event["kind"] for event in replay_frame(sim, seen)["events"]], ["resumed"])

    def test_invalid_windows_and_nonfinite_values_fail_before_running(self):
        for start, duration, sample, rate in ((math.nan, 1, 0.5, 8), (0, math.inf, 0.5, 8),
                                              (0, 1, math.nan, 8), (0, 1, 0.5, math.inf),
                                              (0, 1, 0.05, 8), (0, 1, 0.3, 8)):
            with self.subTest(values=(start, duration, sample, rate)), self.assertRaises(ValueError):
                export_replay(start, duration, sample, rate, 7)
        job = {"scenario": "standard", "seed": 1, "strategy": "locks", "duration": 1}
        with self.assertRaisesRegex(ValueError, "exceeds"):
            export_replay(0.5, 1, 0.5, 8, 7, job=job)

    def test_changing_source_cannot_be_presented_as_reproducible(self):
        with patch("tools.export_3d_replay.source_provenance", side_effect=[{"commit": "a"}, {"commit": "b"}]):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                export_replay(0, 0.5, 0.5, 8, 7)


if __name__ == "__main__":
    unittest.main()
