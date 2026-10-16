"""The dashboard gets tracker-derived motion without changing the established track rows."""
from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path

from fleet.fms import Runtime
from fleet.layout import warehouse_layout
from fleet.perception import Track
from fleet.scenario import get
from fleet.sim import Simulation
from fleet.store import Store


def motion_tracks() -> dict[str, Track]:
    return {
        "T-STOP": Track("T-STOP", "forklift", 10.0, 4.0, vx=1.4, vy=-0.6, last_seen=4.0,
                        heading=0.75, pace=0.82, rest_since=3.0),
        "T-MOVE": Track("T-MOVE", "forklift", 14.0, 8.0, vx=0.7, vy=0.2, last_seen=4.5,
                        heading=-1.25, pace=0.73),
        "T-NEW": Track("T-NEW", "human", 18.0, 12.0, vx=0.5, last_seen=4.8,
                       heading=None, pace=0.51),
    }


class TrackMotionSnapshotTests(unittest.TestCase):
    def assert_motion_contract(self, owner) -> None:
        owner.now = 5.0
        owner.manager.tracker.tracks = motion_tracks()
        before = deepcopy(owner.manager.tracker.tracks)

        encoded = json.loads(json.dumps(owner.snapshot()))

        self.assertEqual(encoded["tracks"], [
            ["T-STOP", "forklift", 10.0, 4.0, 1.4, -0.6, 0.45, 1.0],
            ["T-MOVE", "forklift", 14.0, 8.0, 0.7, 0.2, 0.3, 0.5],
            ["T-NEW", "human", 18.0, 12.0, 0.5, 0.0, 0.21, 0.2],
        ])
        self.assertEqual(encoded["track_motion"], {
            "T-STOP": {"heading": 0.75, "stopped": True, "speed": 0.0},
            "T-MOVE": {"heading": -1.25, "stopped": False, "speed": 0.73},
            "T-NEW": {"heading": None, "stopped": False, "speed": 0.51},
        })
        self.assertEqual(owner.manager.tracker.tracks, before)

    def test_simulation_snapshot_serializes_tracker_motion(self):
        sim = Simulation(get("grid-night", robots=0, external=0), seed=1)
        self.assert_motion_contract(sim)

    def test_runtime_snapshot_serializes_the_same_tracker_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "fms.sqlite3")
            try:
                runtime = Runtime(warehouse_layout(), "127.0.0.1", 1, store, run_id="motion-test")
                self.assert_motion_contract(runtime)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
