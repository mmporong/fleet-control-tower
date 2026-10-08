#!/usr/bin/env bash
# Every result in results/ and docs/BENCHMARK.md, reproduced from the command line.
#
#   cd ~/fleet-control-tower && bash tools/benchmarks.sh [sizing|shift|methods|sensitivity|grid|all]
#
# Each set is one process pool (WORKERS processes); a run is a whole simulation, so the wall time of a set is
# about the slowest run. The JSON files record the git commit (with +dirty when tracked files differ).
set -euo pipefail
cd "$(dirname "$0")/.."
WORKERS=${WORKERS:-15}
WARMUP=${WARMUP:-1800}
B=${BENCH:-"python3 -m fleet.bench --workers $WORKERS --warmup $WARMUP"}   # BENCH: replace the runner (dry runs)
PEAK='"trucks_h": 3.0, "out_pallets_h": 39.0, "tote_orders_h": 103.0'

sizing() {
  # fleet sizing at the design peak (docs/SITE.md section 5): one class varied, the others generous
  $B --scenario standard --seeds 1,2 --duration 3600 --out results/dc_sizing.json --variants "[
    $(for n in 8 10 12 14 16; do printf '{"label": "agf%02d", "scenario_overrides": {%s, "agf": %d, "amr": 24, "pickers": 8}},' $n "$PEAK" $n; done)
    $(for n in 12 16 20 24 28; do printf '{"label": "amr%02d", "scenario_overrides": {%s, "agf": 16, "amr": %d, "pickers": 8}},' $n "$PEAK" $n; done)
    $(for n in 5 6 7; do printf '{"label": "pickers%d", "scenario_overrides": {%s, "agf": 16, "amr": 28, "pickers": %d}},' $n "$PEAK" $n; done)
    {\"label\": \"pickers8\", \"scenario_overrides\": {$PEAK, \"agf\": 16, \"amr\": 28, \"pickers\": 8}}]"
}

shift_runs() {
  # shift length: 8 h measured after the warm-up, the five DC scenarios
  $B --scenario standard --duration 28800 --out results/dc_shift.json --variants '[
    {"label": "standard", "scenario": "standard", "seeds": [1, 2, 3, 4]},
    {"label": "peak", "scenario": "peak", "seeds": [1, 2, 3]},
    {"label": "crowded", "scenario": "crowded", "seeds": [1, 2, 3]},
    {"label": "faults", "scenario": "faults", "seeds": [1, 2, 3]},
    {"label": "night", "scenario": "night", "seeds": [1, 2]}]'
}

methods() {
  # operating methods and traffic strategies on the standard shift (2 h measured)
  $B --scenario standard --strategies none,locks,locks+resolve --seeds 1,2,3 --duration 7200 \
     --out results/dc_methods.json --variants '[
    {"label": "base"},
    {"label": "pick-zone", "strategy": "locks+resolve", "scenario_overrides": {"pick_limit": "zone"}},
    {"label": "pick-vehicle", "strategy": "locks+resolve", "scenario_overrides": {"pick_limit": "vehicle"}},
    {"label": "tote-single", "strategy": "locks+resolve", "scenario_overrides": {"tote_batch": 1}},
    {"label": "dispatch-fifo", "strategy": "locks+resolve", "scenario_overrides": {"dispatch": "fifo"}},
    {"label": "saturated", "strategy": "locks+resolve",
     "scenario_overrides": {"trucks_h": 6.0, "out_pallets_h": 80.0, "tote_orders_h": 200.0}}]'
}

sensitivity() {
  # assumptions swept one at a time on the standard shift (2 h measured)
  $B --scenario standard --seeds 1,2,3 --duration 7200 --out results/dc_sensitivity.json --variants '[
    {"label": "base"},
    {"label": "violation-0", "scenario_overrides": {"violation_p": 0.0}},
    {"label": "violation-10%", "scenario_overrides": {"violation_p": 0.1}},
    {"label": "careless-0", "scenario_overrides": {"careless_p": 0.0}},
    {"label": "careless-30%", "scenario_overrides": {"careless_p": 0.3}},
    {"label": "infra-none", "scenario_overrides": {"infra": "none"}},
    {"label": "infra-full", "scenario_overrides": {"infra": "full"}},
    {"label": "camera-poor", "scenario_overrides": {"cam_detect_p": 0.8, "cam_latency_s": 1.0, "cam_fp_p": 0.05}},
    {"label": "mtbf-8h", "scenario_overrides": {"fault_mtbf_h": 8.0}},
    {"label": "mtbf-72h", "scenario_overrides": {"fault_mtbf_h": 72.0}},
    {"label": "process-errors-0", "scenario_overrides": {"handling_fail_p": 0.0, "inventory_error_p": 0.0}},
    {"label": "process-errors-5%", "scenario_overrides": {"handling_fail_p": 0.05, "inventory_error_p": 0.05}}]'
}

grid() {
  # algorithm test grid (single vehicle class): traffic strategies under deadlock stress, fleet size
  $B --scenario grid-deadlock --strategies none,locks,locks+resolve --seeds 1,2,3 --duration 3600 --warmup 600 \
     --out results/grid_deadlock_strategies.json
  $B --scenario grid-night --seeds 1,2,3 --duration 3600 --warmup 600 --robots 5,10,20,30,40 \
     --out results/grid_fleet_size.json
  $B --scenario grid-night --seeds 1,2,3 --duration 3600 --warmup 600 --robots 10,20,30,40,50 \
     --variants '[{"label": "saturated", "scenario_overrides": {"rate_per_h": 600}}]' \
     --out results/grid_fleet_size_saturated.json
  $B --scenario grid --strategies none,locks+resolve --seeds 1,2,3 --duration 3600 --warmup 600 \
     --out results/grid_mixed.json
}

case "${1:-all}" in
  sizing) sizing ;;
  shift) shift_runs ;;
  methods) methods ;;
  sensitivity) sensitivity ;;
  grid) grid ;;
  all) sizing; shift_runs; methods; sensitivity; grid ;;
  *) echo "usage: $0 [sizing|shift|methods|sensitivity|grid|all]" >&2; exit 2 ;;
esac
