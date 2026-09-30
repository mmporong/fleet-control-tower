"""Export a site layout as a VDMA LIF file and its site sidecar.

    python3 tools/export_lif.py --site dc --out sites/dc.lif.json       # sidecar: sites/dc.site.json
    python3 tools/export_lif.py --site grid --out sites/grid.lif.json

The LIF holds what a third-party master control system routes on: nodes,
directed edges with per-vehicle-type access and speed, and stations. Zones,
racks, camera coverage, doors, walls, no-stop nodes and node kinds go to the
sidecar, since LIF 1.0.0 has no slot for them (fleet/lif.py).

An unchanged LIF keeps its layoutVersion and exportTimestamp and is not
rewritten; a changed one gets layoutVersion + 1 and a new timestamp.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet.layout import warehouse_layout  # noqa: E402
from fleet.lif import export_lif, export_site_extras, lif_timestamp, validate_lif  # noqa: E402
from fleet.site_dc import dc_layout  # noqa: E402

# vehicleTypeId per VDA 5050 agvClass. Neither vehicle turns while driving an edge: the tote AMR turns
# on the spot at nodes (tote_amr_spec), the forklift's turn is modelled at nodes too (agf_spec).
SITES = {
    "dc": {"build": dc_layout, "layoutId": "dc", "name": "팔레트 물류센터 88×53.5 m",
           "vehicleTypes": {"FORKLIFT": {"vehicleTypeId": "AGF-CB15", "rotationAllowed": False},
                            "CARRIER": {"vehicleTypeId": "AMR-TOTE", "rotationAllowed": False}}},
    "grid": {"build": warehouse_layout, "layoutId": "grid", "name": "격자 창고 60×34 m",
             "vehicleTypes": {"CARRIER": {"vehicleTypeId": "AMR-GENERIC", "rotationAllowed": False}}},
}


def site_documents(site: str, version: str = "1", timestamp: str | None = None) -> tuple[dict, dict]:
    """(LIF document, sidecar) for a named site."""
    cfg = SITES[site]
    layout = cfg["build"]()
    lif = export_lif(layout, cfg["vehicleTypes"], cfg["layoutId"], cfg["name"], version, timestamp=timestamp)
    extras = export_site_extras(layout, cfg["layoutId"], cfg["vehicleTypes"])
    # through JSON so the result compares equal to a file read back (tuples become lists)
    return json.loads(json.dumps(lif)), json.loads(json.dumps(extras))


def _content(lif: dict) -> dict:
    """The document without the fields that only record when and how often it was exported."""
    meta = {k: v for k, v in lif["metaInformation"].items() if k != "exportTimestamp"}
    return {**lif, "metaInformation": meta,
            "layouts": [{k: v for k, v in lay.items() if k != "layoutVersion"} for lay in lif["layouts"]]}


def _write(path: Path, doc: dict) -> str:
    text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "updated"


def export(site: str, out: Path, extras_out: Path) -> str:
    lif, extras = site_documents(site)
    problems = validate_lif(lif)
    if problems:
        raise SystemExit(f"{site}: exported LIF is invalid: {problems[:5]}")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    if old is not None and _content(old) == _content(lif):
        version, stamp = old["layouts"][0]["layoutVersion"], old["metaInformation"]["exportTimestamp"]
    else:
        prev = str(old["layouts"][0]["layoutVersion"]) if old else "0"
        if not prev.isdigit():
            raise SystemExit(f"{out}: layoutVersion {prev!r} is not an integer; renumber it before exporting")
        version, stamp = str(int(prev) + 1), lif_timestamp()
    lif["layouts"][0]["layoutVersion"] = version
    lif["metaInformation"]["exportTimestamp"] = stamp
    return (f"{site}: LIF {_write(out, lif)} (layoutVersion {version}) -> {out}; "
            f"sidecar {_write(extras_out, extras)} -> {extras_out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", choices=sorted(SITES), required=True)
    ap.add_argument("--out", type=Path, required=True, help="LIF file, e.g. sites/dc.lif.json")
    ap.add_argument("--extras-out", type=Path, help="sidecar file (default: sites/dc.lif.json -> sites/dc.site.json)")
    args = ap.parse_args()
    stem = args.out.name.removesuffix(".json").removesuffix(".lif")
    print(export(args.site, args.out, args.extras_out or args.out.with_name(stem + ".site.json")))


if __name__ == "__main__":
    main()
