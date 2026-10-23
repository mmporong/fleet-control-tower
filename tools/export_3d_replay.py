"""Export simulator snapshots for the read-only, static 3D replay.

    python3 tools/export_3d_replay.py --out /tmp/fleet-replay.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet.scenario import get  # noqa: E402
from fleet.sim import Simulation  # noqa: E402


def export_replay(start: float, duration: float, sample: float, rate: float, seed: int) -> dict:
    sim = Simulation(get("standard"), seed=seed, strategy="locks+resolve")
    for name, value in (("start", start), ("duration", duration), ("sample", sample)):
        if value < 0 or (name != "start" and value == 0):
            raise ValueError(f"{name} must be {'nonnegative' if name == 'start' else 'positive'}")
        if abs(value / sim.dt - round(value / sim.dt)) > 1e-7:
            raise ValueError(f"{name} must be a multiple of simulator dt ({sim.dt})")
    if rate <= 0:
        raise ValueError("rate must be positive")
    start_step = round(start / sim.dt)
    stop_step = round((start + duration) / sim.dt)
    interval = round(sample / sim.dt)
    if (stop_step - start_step) % interval:
        raise ValueError("duration must be a multiple of sample")
    fields = ("t", "scenario", "scenario_title", "layout_key", "mode", "robots",
              "truth", "site_state", "alarm_counts", "kpi")
    robot_fields = ("id", "cls", "x", "y", "th", "v", "len", "wid", "st", "label",
                    "bat", "load", "conn", "purpose", "goal", "base", "route", "positioned")
    frames = []
    for step in range(stop_step + 1):
        if step >= start_step and (step - start_step) % interval == 0:
            raw = sim.snapshot()
            frame = {key: raw[key] for key in fields}
            frame["robots"] = [{key: robot[key] for key in robot_fields} for robot in raw["robots"]]
            frames.append(frame)
        if step < stop_step:
            sim.step()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {"version": 1, "playbackRate": rate, "sampleInterval": sample,
            "layout": sim.layout.to_json(), "frames": frames,
            "source": {"scenario": "standard", "seed": seed, "strategy": "locks+resolve",
                       "startTime": frames[0]["t"], "endTime": frames[-1]["t"], "commit": commit}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=float, default=120)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--sample", type=float, choices=(0.5,), default=0.5)
    parser.add_argument("--rate", type=float, choices=(8,), default=8)
    parser.add_argument("--seed", type=int, choices=(7,), default=7)
    args = parser.parse_args()
    data = export_replay(args.start, args.duration, args.sample, args.rate, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Exported {len(data['frames'])} snapshots: {data['source']['startTime']}–"
          f"{data['source']['endTime']} s, playback {args.rate:g}×, {args.out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
