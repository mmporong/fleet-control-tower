"""Export simulator snapshots for the read-only, static 3D replay.

    python3 tools/export_3d_replay.py --out /tmp/fleet-replay.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# PYTHONPATH may point at an immutable baseline checkout for before/after exports.
sys.path.append(str(ROOT))
import fleet  # noqa: E402
from fleet.autoop import AutoOperator  # noqa: E402
from fleet.control import ManagerConfig  # noqa: E402
from fleet.ops import Operations  # noqa: E402
from fleet.scenario import get  # noqa: E402
from fleet.sim import Simulation  # noqa: E402

SOURCE_ROOT = Path(fleet.__file__).resolve().parents[1]


def source_provenance() -> dict:
    """Identify the code actually imported, including uncommitted simulator changes."""
    sources = {str(p.relative_to(SOURCE_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((SOURCE_ROOT / "fleet").glob("*.py"))}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "fleet"],
        cwd=SOURCE_ROOT, text=True).strip())
    return {"commit": commit, "dirty": dirty, "files": sources,
            "exporterSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def replay_frame(sim: Simulation, seen_events: set[tuple] | None = None) -> dict:
    raw = sim.snapshot()
    fields = ("t", "scenario", "scenario_title", "layout_key", "mode", "robots",
              "truth", "site_state", "alarm_counts", "kpi", "locks", "lanes", "blocks")
    robot_fields = ("id", "cls", "x", "y", "th", "v", "len", "wid", "st", "label",
                    "bat", "load", "conn", "purpose", "goal", "base", "route", "positioned",
                    "why", "wait_s", "wait_kind", "wait_holder", "task", "stop")
    frame = {key: raw[key] for key in fields}
    frame["robots"] = []
    for robot in raw["robots"]:
        item = {key: robot[key] for key in robot_fields}
        wait = sim.manager.robots[robot["id"]].wait
        item["wait_resource"] = wait.resource if wait else None
        frame["robots"].append(item)
    frame["events"] = []
    for event in raw["events"]:
        key = (event["t"], event["type"], event["source"], event["msg"], event["level"])
        if seen_events is not None:
            if key in seen_events:
                continue
            seen_events.add(key)
        frame["events"].append({"t": event["t"], "kind": event["type"], "source": event["source"],
                                "msg": event["msg"], "level": event["level"]})
    return frame


def measurement_summary(sim: Simulation, job: dict) -> dict:
    kpi = sim.kpi(window=math.inf)
    site = kpi["site"]
    return {"durationSeconds": job["duration"], "warmupSeconds": job.get("warmup", 0),
            "putawayPerHour": site.get("putaway_h", 0), "shippedPerHour": site.get("shipped_h", 0),
            "totePerHour": site.get("tote_h", 0), "queued": kpi["queued"],
            "contacts": kpi["contacts"], "movingContacts": kpi["contacts_moving"],
            "lockProblems": len(sim.manager.locks.check()), "mismatch": kpi["mismatch"]}


def export_replay(start: float, duration: float, sample: float, rate: float, seed: int,
                  *, job: dict | None = None) -> dict:
    """Capture absolute simulation times; a benchmark job continues to its full measurement end.

    With a job, seed/scenario/operator/overrides/warm-up come from that job. The
    final summary covers its entire duration, never just the recorded window.
    Without a job the original operator-free standard reference is preserved.
    """
    before = source_provenance()
    spec = dict(job) if job is not None else {
        "scenario": "standard", "seed": seed, "strategy": "locks+resolve", "operator": False,
        "warmup": 0, "duration": start + duration,
    }
    if not isinstance(spec["seed"], int) or isinstance(spec["seed"], bool):
        raise ValueError("job seed must be an integer")
    if spec["strategy"] not in ("none", "locks", "locks+resolve"):
        raise ValueError("unsupported strategy")
    sim = Simulation(get(spec["scenario"], **spec.get("scenario_overrides", {})), seed=spec["seed"],
                     strategy=spec["strategy"], config=ManagerConfig(**spec.get("manager", {})),
                     vehicle_overrides=spec.get("vehicle") or None)
    warmup = float(spec.get("warmup", 0))
    measured = float(spec["duration"])
    for name, value in (("start", start), ("duration", duration), ("sample", sample)):
        if not math.isfinite(value) or value < 0 or (name != "start" and value == 0):
            raise ValueError(f"{name} must be {'nonnegative' if name == 'start' else 'positive'}")
        if abs(value / sim.dt - round(value / sim.dt)) > 1e-7:
            raise ValueError(f"{name} must be a multiple of simulator dt ({sim.dt})")
    for name, value in (("warmup", warmup), ("measurement duration", measured)):
        if not math.isfinite(value) or value < 0 or (name != "warmup" and value == 0):
            raise ValueError(f"invalid {name}")
        if abs(value / sim.dt - round(value / sim.dt)) > 1e-7:
            raise ValueError(f"{name} must be a multiple of simulator dt ({sim.dt})")
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("rate must be positive")
    if start + duration > warmup + measured + 1e-7:
        raise ValueError("recorded window exceeds benchmark job duration")
    start_step = round(start / sim.dt)
    stop_step = round((start + duration) / sim.dt)
    warmup_step = round(warmup / sim.dt)
    final_step = round((warmup + measured) / sim.dt)
    interval = round(sample / sim.dt)
    if (stop_step - start_step) % interval:
        raise ValueError("duration must be a multiple of sample")
    ops = Operations(sim)
    auto = AutoOperator(ops, random.Random(spec["seed"] + 99))
    if spec.get("operator", True):
        sim.tick_hooks.append(auto.tick)
    frames = []
    seen_events: set[tuple] = set()
    for step in range(final_step + 1):
        if warmup_step > 0 and step == warmup_step:
            sim.reset_measurement()
            auto.ack_times.clear()
            ops.counts = {key: 0 for key in ops.counts}
        if start_step <= step <= stop_step and (step - start_step) % interval == 0:
            frames.append(replay_frame(sim, seen_events))
        if step < final_step:
            sim.step()
    after = source_provenance()
    if before != after:
        raise RuntimeError("source changed during export; rerun from an immutable checkout")
    return {"version": 1, "playbackRate": rate, "sampleInterval": sample,
            "layout": sim.layout.to_json(), "frames": frames,
            "source": {"scenario": spec["scenario"], "seed": spec["seed"], "strategy": spec["strategy"],
                       "startTime": frames[0]["t"], "endTime": frames[-1]["t"], **before, "job": spec},
            "measurement": {"summary": measurement_summary(sim, spec),
                            "startTime": warmup, "endTime": round(sim.now, 1)}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=float, default=120)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--sample", type=float, default=0.5)
    parser.add_argument("--rate", type=float, default=8)
    parser.add_argument("--seed", type=int, default=7, help="reference seed; a job file supplies its own seed")
    parser.add_argument("--job", type=Path, help="benchmark job JSON, including warmup, duration and operator")
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8")) if args.job else None
    data = export_replay(args.start, args.duration, args.sample, args.rate, args.seed, job=job)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print(f"Exported {len(data['frames'])} snapshots: {data['source']['startTime']}–"
          f"{data['source']['endTime']} s, playback {args.rate:g}×, {args.out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
