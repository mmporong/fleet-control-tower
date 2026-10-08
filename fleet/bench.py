"""Headless benchmark: compare traffic strategies and settings over seeds.

Each run is one process (multiprocessing), so a sweep uses the cores of the
machine. Results are JSON with every KPI per run plus the configuration, so
numbers in reports can be traced back to a command line.

    python3 -m fleet.bench --scenario standard --strategies locks+resolve,locks,none \\
        --seeds 1,2,3 --duration 3600 --out results/strategies.json
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import time

from .autoop import AutoOperator
from .control import ManagerConfig
from .ops import Operations
from .scenario import PRESETS, get
from .sim import Simulation

KEY_METRICS = ("done", "throughput_h", "cycle_avg", "cycle_p90", "wait_avg", "exec_avg", "traffic_wait_ratio",
               "utilization", "deadlocks", "deadlocks_resolved", "reroutes", "evasions", "protective_stops",
               "protective_human", "near_miss_fms", "contacts", "contacts_moving", "contacts_struck",
               "contacts_static", "contacts_human", "contacts_human_moving", "near_misses", "close_calls", "clearance_violations",
               "min_gap_moving", "min_amr_gap", "careless_turns", "tracking_ratio", "mismatch", "alarms_per_h",
               "mttr_s", "mtta_s", "interventions", "queued", "site_putaway_h", "site_shipped_h", "site_tote_h",
               "site_picks", "site_units", "site_dock_to_stock_avg", "site_dock_to_stock_p90", "site_picker_wait_s",
               "site_staged_in", "site_tote_backlog", "site_exceptions", "site_rack_fill", "agf_moves_per_vehicle_h",
               "agf_m_per_move", "amr_orders_per_vehicle_h", "picker_lph", "picker_uph")


def run_one(job: dict) -> dict:
    sc = get(job["scenario"], **job.get("scenario_overrides", {}))
    cfg = ManagerConfig(**job.get("manager", {}))
    t0 = time.time()
    # vehicle overrides change only the named fields of each class's own spec (AGF, tote AMR, grid AMR)
    sim = Simulation(sc, seed=job["seed"], strategy=job["strategy"], config=cfg,
                     vehicle_overrides=job.get("vehicle") or None)
    ops = Operations(sim)
    auto = AutoOperator(ops, random.Random(job["seed"] + 99))
    if job.get("operator", True):
        sim.tick_hooks.append(auto.tick)
    warmup = float(job.get("warmup", 0.0))
    if warmup > 0:
        sim.run(warmup)
        sim.reset_measurement()
        auto.ack_times.clear()
        ops.counts = {k: 0 for k in ops.counts}
    sim.run(job["duration"])
    kpi = sim.kpi()
    for key, value in (kpi.pop("site", {}) or {}).items():
        kpi[f"site_{key}"] = value
    prod = kpi.pop("productivity", {}) or {}
    agf, amr, pick = prod.get("FORKLIFT") or {}, prod.get("CARRIER") or {}, prod.get("pickers") or {}
    kpi.update(agf_moves_per_vehicle_h=agf.get("moves_per_vehicle_h"), agf_m_per_move=agf.get("m_per_move"),
               amr_orders_per_vehicle_h=amr.get("orders_per_vehicle_h"), picker_lph=pick.get("lph"),
               picker_uph=pick.get("uph"))
    kpi["time_share"] = {cls: v.get("time_share") for cls, v in prod.items() if isinstance(v, dict) and "time_share" in v}
    kpi["interventions"] = ops.counts["interventions"]
    kpi["mtta_s"] = round(statistics.mean(auto.ack_times), 1) if auto.ack_times else None
    kpi["lock_problems"] = len(sim.manager.locks.check())
    return {"job": job, "kpi": kpi, "wall_s": round(time.time() - t0, 1),
            "forklift_reversals": [getattr(f, "reversals", 0) for f in sim.forklifts]}


# two-sided 95 % Student t quantiles by degrees of freedom (n - 1)
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
       11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 19: 2.093, 24: 2.064, 29: 2.045}


def ci95(values: list[float]) -> float:
    """Half-width of the 95 % confidence interval of the mean (Student t).

    >>> round(ci95([10.0, 12.0, 14.0]), 2)
    4.97
    """
    n = len(values)
    if n < 2:
        return 0.0
    df = n - 1
    t = T95.get(df) or T95[max(k for k in T95 if k <= df)] if df <= 29 else 1.96
    return t * statistics.stdev(values) / n ** 0.5


def summarize(results: list[dict], group_key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["job"][group_key] if group_key in r["job"] else r["job"]["label"], []).append(r)
    out = {}
    for g, rs in groups.items():
        row = {"runs": len(rs)}
        for k in KEY_METRICS:
            vals = [r["kpi"][k] for r in rs if r["kpi"].get(k) is not None]
            if vals:
                row[k] = {"mean": round(statistics.mean(vals), 3),
                          "sd": round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0,
                          "ci95": round(ci95(vals), 3), "min": min(vals), "max": max(vals)}
        out[g] = row
    return out


def git_rev() -> str:
    """Short commit id, with ``+dirty`` when tracked files differ from it (numbers then are not reproducible)."""
    root = Path(__file__).resolve().parents[1]
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=root, timeout=5).stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, cwd=root, timeout=5).stdout.strip()
        return rev + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="standard", choices=sorted(PRESETS))
    ap.add_argument("--strategies", default="locks+resolve")
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--duration", type=float, default=3600.0, help="measured seconds (after the warm-up)")
    ap.add_argument("--warmup", type=float, default=1800.0, help="seconds run before measuring starts")
    ap.add_argument("--robots", default="", help="comma list: fleet-size sweep (overrides scenario)")
    ap.add_argument("--variants", default="",
                    help="JSON list of {label, scenario, seeds, strategy, manager, vehicle, scenario_overrides}; "
                         "scenario and seeds default to --scenario and --seeds, strategy to every --strategies")
    ap.add_argument("--no-operator", action="store_true", help="run without the simulated operator")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    seeds = [int(s) for s in args.seeds.split(",") if s]
    variants = json.loads(args.variants) if args.variants else [{"label": "default"}]
    robots = [int(r) for r in args.robots.split(",") if r]
    jobs = []
    for strategy in args.strategies.split(","):
        for var in variants:
            if var.get("strategy", strategy) != strategy:
                continue                    # a variant that names its strategy runs only under that one
            for n in robots or [None]:
                for seed in var.get("seeds", seeds):
                    overrides = dict(var.get("scenario_overrides", {}))
                    if n is not None:
                        overrides["robots"] = n
                    label = "/".join(x for x in (strategy, var.get("label", "default"),
                                                 f"{n} robots" if n else "") if x)
                    jobs.append({"scenario": var.get("scenario", args.scenario), "strategy": strategy, "seed": seed,
                                 "duration": args.duration, "warmup": args.warmup, "manager": var.get("manager", {}),
                                 "vehicle": var.get("vehicle", {}), "scenario_overrides": overrides,
                                 "operator": not args.no_operator, "label": label})
    rev = git_rev()
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_one, jobs))
    summary = summarize(results, "label")
    doc = {"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "git": rev, "python": platform.python_version(),
           "scenario": PRESETS[args.scenario].to_json(), "duration_s": args.duration, "warmup_s": args.warmup,
           "seeds": seeds,
           "wall_s": round(time.time() - t0, 1), "summary": summary, "runs": results}
    for label, row in summary.items():
        def m(k):
            v = row.get(k)
            return "-" if v is None else f"{v['mean']:.2f}±{v['sd']:.2f}"
        site = (f" putaway/h {m('site_putaway_h')} ship/h {m('site_shipped_h')} tote/h {m('site_tote_h')} "
                f"d2s {m('site_dock_to_stock_avg')}") if "site_putaway_h" in row else ""
        print(f"{label:40s} thr/h {m('throughput_h'):>14} wait {m('traffic_wait_ratio'):>11} "
              f"deadlock {m('deadlocks'):>11} resolved {m('deadlocks_resolved'):>11} "
              f"contact {m('contacts'):>9} nearmiss {m('near_misses'):>11}{site}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
