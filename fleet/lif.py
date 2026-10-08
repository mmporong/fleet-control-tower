"""VDMA LIF (Layout Interchange Format) 1.0.0: export a Layout and import one back.

LIF is how a vehicle integrator hands a track layout to a (third-party) master
control system: nodes, directed edges with per-vehicle-type access and speed,
and stations. Mapping to ``Layout``:

- a two-way lane is two directed LIF edges, a one-way lane is one; LIF has no
  bidirectional edge, and the lane key is the sorted pair of node ids;
- ``Edge.classes`` (VDA 5050 agvClass) -> one vehicleTypeEdgeProperty per
  allowed vehicle type, each with ``maxSpeed = Edge.max_speed``;
- a node lists every vehicle type that may drive an edge touching it;
- stop and work nodes (docks, staging lanes, rack and pick faces, pack
  stations, chargers, parking bays) -> one station each, carrying the actions
  this fleet manager sends there, because LIF 8.3.13.1 defines a station's
  purpose by the actions on its interaction node.

Zones, racks, camera coverage, doors, walls, no-stop nodes, node kinds and the
vehicleTypeId -> agvClass map have no LIF member. LIF 1.0.0 defines no slot for
integrator data: new members come from the VDMA change process (3, 6), doors and
other periphery are agreed outside the LIF (7.2), and the 2.0 draft (FAQ 10.2)
says such data needs a parallel document instead of extra fields. They go to a
sidecar document, ``export_site_extras``, keyed by the ids used in the LIF.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import warnings

from .facility import DoorSpec
from .geometry import Point
from .layout import Edge, Layout, Node, Zone
from .vda5050 import MAP_ID

LIF_VERSION = "1.0.0"
SUPPORTED_MAJORS = ("0", "1")        # the 1.0.0 specification's own examples say "0.11.0"
CREATOR = "fleet-control-tower"
EXTRAS_FORMAT = "fleet-control-tower/site-extras"
EXTRAS_VERSION = "1"

# Actions the fleet manager sends at each kind of stop (dispatcher.py, process.py, process_dc.py).
# All of them block (HARD) and depend on the task, hence CONDITIONAL.
STATION_ACTIONS: dict[str, tuple[str, ...]] = {
    "inbound": ("pick",), "outbound": ("drop",), "storage": ("pick", "drop"),
    "in_stage": ("pick",), "out_stage": ("drop",), "rack": ("pick", "drop"),
    "pack": ("x_toteInduct", "x_toteDrop"), "pickface": ("x_pickItems",),
    "charger": ("startCharging", "stopCharging"), "park": (),
}
# Optional vehicleTypeEdgeProperty members a caller may set per vehicle type in ``vehicle_types``
EDGE_PROPERTY_KEYS = ("vehicleOrientation", "orientationType", "rotationAtStartNodeAllowed",
                      "rotationAtEndNodeAllowed", "maxRotationSpeed", "minHeight", "maxHeight", "reentryAllowed")
# sidecar members and their JSON container type
EXTRAS_SHAPES = {"vehicleTypes": dict, "nodeKinds": dict, "noStop": list, "edges": dict, "zones": list,
                 "racks": list, "coverage": dict, "doors": list, "walls": list}


class LIFError(ValueError):
    """A LIF document or its sidecar cannot be turned into a Layout."""


class LIFWarning(UserWarning):
    """A LIF member the importer cannot represent in a Layout, so it is not applied (LIF 8.1.1)."""


def lif_timestamp(when: datetime | None = None) -> str:
    """ISO 8601 in UTC with two fractional digits, as LIF 8.3.2 writes it.

    >>> lif_timestamp(datetime(2017, 4, 15, 11, 40, 3, 120000, tzinfo=timezone.utc))
    '2017-04-15T11:40:03.12Z'
    """
    t = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 10000:02d}Z"


def lane_key(a: str, b: str) -> str:
    """Undirected lane shared by both directions of a two-way lane.

    >>> lane_key("SW50", "SE50") == lane_key("SE50", "SW50")
    True
    """
    return "|".join(sorted((a, b)))


def _length(nodes: dict[str, Node], a: str, b: str) -> float:
    na, nb = nodes[a], nodes[b]
    return round(math.hypot(na.x - nb.x, na.y - nb.y), 3)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _points(poly) -> list[list[float]]:
    return [[x, y] for x, y in poly]


def _tuples(poly) -> list[Point]:
    return [(p[0], p[1]) for p in poly]


# ---- export ------------------------------------------------------------------
def _type_ids(vehicle_types: dict[str, dict]) -> dict[str, str]:
    if not vehicle_types:
        raise ValueError("vehicle_types is empty: LIF needs at least one vehicle type")
    ids = {}
    for cls, vt in vehicle_types.items():
        vid = vt.get("vehicleTypeId")
        if not isinstance(vid, str) or not vid:
            raise ValueError(f"vehicle_types[{cls!r}] has no vehicleTypeId")
        ids[cls] = vid
    if len(set(ids.values())) != len(ids):
        raise ValueError("vehicleTypeId values must be unique")
    return ids


def export_lif(layout: Layout, vehicle_types: dict[str, dict], layout_id: str, name: str, version: str,
               *, timestamp: str | None = None) -> dict:
    """LIF document with ``layout`` as its single layout.

    ``vehicle_types`` maps agvClass -> {"vehicleTypeId": ..., optional edge members such as
    "rotationAllowed" (default false) or "maxHeight"}. An edge without class restriction is open to
    every type listed; a restricted edge only to its classes, each of which must be listed.
    """
    type_ids = _type_ids(vehicle_types)
    edge_classes: dict[str, list[str]] = {}
    for e in layout.edges.values():
        unknown = sorted(set(e.classes) - set(vehicle_types))
        if unknown:
            raise ValueError(f"edge {e.id} allows {unknown}, which vehicle_types does not list")
        edge_classes[e.id] = [c for c in vehicle_types if not e.classes or c in e.classes]
    node_classes: dict[str, set[str]] = {n: set() for n in layout.nodes}
    for e in layout.edges.values():
        node_classes[e.start].update(edge_classes[e.id])
        node_classes[e.end].update(edge_classes[e.id])

    nodes, stations = [], []
    for n in layout.nodes.values():
        # LIF forbids an empty list; a node no edge touches stays open to every type
        classes = [c for c in vehicle_types if c in node_classes[n.id]] or list(vehicle_types)
        actions = [{"actionType": a, "requirementType": "CONDITIONAL", "blockingType": "HARD"}
                   for a in STATION_ACTIONS.get(n.kind, ())]
        props = []
        for c in classes:
            prop: dict = {"vehicleTypeId": type_ids[c]}
            if actions:
                prop["actions"] = [dict(a) for a in actions]
            props.append(prop)
        nodes.append({"nodeId": n.id, "mapId": MAP_ID, "nodePosition": {"x": n.x, "y": n.y},
                      "vehicleTypeNodeProperties": props})
        if n.kind in STATION_ACTIONS:
            stations.append({"stationId": n.id, "interactionNodeIds": [n.id]})

    edges = []
    for e in layout.edges.values():
        props = []
        for c in edge_classes[e.id]:
            vt = vehicle_types[c]
            prop = {"vehicleTypeId": type_ids[c], "rotationAllowed": bool(vt.get("rotationAllowed", False)),
                    "maxSpeed": e.max_speed}
            prop.update({k: vt[k] for k in EDGE_PROPERTY_KEYS if k in vt})
            props.append(prop)
        edges.append({"edgeId": e.id, "startNodeId": e.start, "endNodeId": e.end, "vehicleTypeEdgeProperties": props})

    return {
        "metaInformation": {"projectIdentification": name, "creator": CREATOR,
                            "exportTimestamp": timestamp or lif_timestamp(), "lifVersion": LIF_VERSION},
        "layouts": [{"layoutId": layout_id, "layoutName": name, "layoutVersion": version,
                     "nodes": nodes, "edges": edges, "stations": stations}],
    }


def export_site_extras(layout: Layout, layout_id: str | None = None,
                       vehicle_types: dict[str, dict] | None = None) -> dict:
    """Sidecar with everything in ``layout`` that LIF 1.0.0 cannot carry.

    ``vehicle_types`` is the mapping given to ``export_lif``; without it the importer cannot turn
    vehicleTypeIds back into agvClasses. Lane key, length, one-way flag and class set of an edge are
    derived on import (sorted node pair, straight-line length, no reverse edge, types -> classes);
    ``edges`` lists only the edges where that would be wrong.
    """
    declared = set(vehicle_types or ())
    pairs = {(e.start, e.end) for e in layout.edges.values()}
    overrides = {}
    for e in layout.edges.values():
        diff: dict = {}
        if e.lane != lane_key(e.start, e.end):
            diff["lane"] = e.lane
        if e.length != _length(layout.nodes, e.start, e.end):
            diff["length"] = e.length
        if e.oneway == ((e.end, e.start) in pairs):
            diff["oneway"] = e.oneway
        # an edge naming every declared class imports as unrestricted; keep the explicit set
        if declared and e.classes and set(e.classes) >= declared:
            diff["classes"] = sorted(e.classes)
        if diff:
            overrides[e.id] = diff
    return {
        "format": EXTRAS_FORMAT,
        "formatVersion": EXTRAS_VERSION,
        "layoutId": layout_id,
        "site": layout.site,
        "width": layout.width,
        "height": layout.height,
        "twoWay": layout.two_way,
        "vehicleTypes": {vt["vehicleTypeId"]: c for c, vt in (vehicle_types or {}).items()},
        "nodeKinds": {n.id: n.kind for n in layout.nodes.values()},
        "noStop": sorted(layout.no_stop),
        "edges": overrides,
        "zones": [{"id": z.id, "name": z.name, "kind": z.kind, "polygon": _points(z.polygon),
                   "staticSpeed": z.static_speed, "humanSpeed": z.human_speed, "locked": z.locked,
                   "collaborative": z.collaborative}
                  for z in layout.zones.values()],
        "racks": [_points(r) for r in layout.racks],
        "coverage": {k: _points(v) for k, v in layout.coverage.items()},
        "doors": [d.to_json() for d in layout.doors.values()],
        "walls": [_points(w) for w in layout.walls],
    }


# ---- validation --------------------------------------------------------------
def _objects(parent: dict, key: str, where: str, problems: list[str], required: bool = True) -> list[dict]:
    value = parent.get(key)
    if value is None:
        if required:
            problems.append(f"{where}: {key} missing")
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        problems.append(f"{where}: {key} must be an array of objects")
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
    return value


def _vehicle_types(obj: dict, key: str, where: str, problems: list[str]) -> list[str]:
    props = _objects(obj, key, where, problems)
    if obj.get(key) == []:
        problems.append(f"{where}: {key} must not be empty")
    types = [p.get("vehicleTypeId") for p in props]
    good = [t for t in types if isinstance(t, str)]
    if len(good) != len(types):
        problems.append(f"{where}: {key} entry without a string vehicleTypeId")
    if len(set(good)) != len(good):
        problems.append(f"{where}: a vehicle type appears twice in {key}")
    for p in props:
        _objects(p, "actions", where, problems, required=False)
    return good


def _known(node_id, table: dict | set) -> bool:
    return isinstance(node_id, str) and node_id in table


def validate_lif(doc: dict) -> list[str]:
    """Structure the importer reads, checked in code (the runtime has no JSON schema library).

    An empty list means importable. Checked: lifVersion, non-empty layouts and nodes, ids unique across
    all layouts (8.2, 8.3.4), node positions, edges and stations pointing at existing nodes, non-empty
    duplicate-free vehicle type lists, and edge vehicle types admitted by both end nodes (implied by
    8.3.4 in 1.0.0, a "shall" in the 2.0 draft).
    """
    if not isinstance(doc, dict):
        return ["document is not a JSON object"]
    problems: list[str] = []
    meta = doc.get("metaInformation")
    if not isinstance(meta, dict):
        problems.append("metaInformation missing")
    else:
        version = meta.get("lifVersion")
        if not isinstance(version, str) or version.split(".")[0] not in SUPPORTED_MAJORS:
            problems.append(f"lifVersion {version!r} is not supported (this importer reads {LIF_VERSION})")
    layouts = _objects(doc, "layouts", "document", problems)
    if doc.get("layouts") == []:
        problems.append("layouts must not be empty")
    node_types: dict[str, set[str]] = {}
    layout_nodes: dict[str, set[str]] = {}
    for lay in layouts:
        lid = lay.get("layoutId")
        if not isinstance(lid, str):
            problems.append(f"layout with layoutId {lid!r}: layoutId must be a string")
            continue
        if lid in layout_nodes:
            problems.append(f"duplicate layoutId {lid}")
        if "layoutVersion" not in lay:
            problems.append(f"layout {lid}: layoutVersion missing")
        if lay.get("nodes") == []:
            problems.append(f"layout {lid} has no nodes")
        own = layout_nodes.setdefault(lid, set())
        for n in _objects(lay, "nodes", f"layout {lid}", problems):
            nid = n.get("nodeId")
            if not isinstance(nid, str):
                problems.append(f"layout {lid}: node without a string nodeId")
                continue
            if nid in node_types:
                problems.append(f"duplicate nodeId {nid}")
            pos = n.get("nodePosition")
            if not (isinstance(pos, dict) and _is_number(pos.get("x")) and _is_number(pos.get("y"))):
                problems.append(f"node {nid}: nodePosition needs numeric x and y")
            node_types[nid] = set(_vehicle_types(n, "vehicleTypeNodeProperties", f"node {nid}", problems))
            own.add(nid)

    edge_ids: set[str] = set()
    station_ids: set[str] = set()
    for lay in layouts:
        lid = lay.get("layoutId")
        if not isinstance(lid, str):
            continue
        for e in _objects(lay, "edges", f"layout {lid}", problems):
            eid = e.get("edgeId")
            if not isinstance(eid, str):
                problems.append(f"layout {lid}: edge without a string edgeId")
                continue
            if eid in edge_ids:
                problems.append(f"duplicate edgeId {eid}")
            edge_ids.add(eid)
            s, t = e.get("startNodeId"), e.get("endNodeId")
            if not _known(s, node_types):
                problems.append(f"edge {eid}: start node {s} is unknown")
            elif s not in layout_nodes[lid]:
                problems.append(f"edge {eid}: start node {s} is not in layout {lid}")
            if not _known(t, node_types):
                problems.append(f"edge {eid}: end node {t} is unknown")
            for vt in _vehicle_types(e, "vehicleTypeEdgeProperties", f"edge {eid}", problems):
                problems += [f"edge {eid}: node {end} does not admit vehicle type {vt}"
                             for end in (s, t) if _known(end, node_types) and vt not in node_types[end]]
        # stations are optional in the specification text (8.3.3), although the schema requires them
        for st in _objects(lay, "stations", f"layout {lid}", problems, required=False):
            sid = st.get("stationId")
            if not isinstance(sid, str):
                problems.append(f"layout {lid}: station without a string stationId")
                continue
            if sid in station_ids:
                problems.append(f"duplicate stationId {sid}")
            station_ids.add(sid)
            ids = st.get("interactionNodeIds")
            if not isinstance(ids, list) or not ids:
                problems.append(f"station {sid}: interactionNodeIds must list at least one node")
                continue
            problems += [f"station {sid}: interaction node {nid} is not in layout {lid}"
                         for nid in ids if not _known(nid, layout_nodes[lid])]
    return problems


def _summary(title: str, problems: list[str], shown: int = 8) -> str:
    more = f" (+{len(problems) - shown} more)" if len(problems) > shown else ""
    return f"{title}: " + "; ".join(problems[:shown]) + more


def _extras_problems(extras: dict, lay: dict) -> list[str]:
    lid = lay["layoutId"]
    problems = []
    if extras.get("format") != EXTRAS_FORMAT:
        problems.append(f"format is {extras.get('format')!r}, expected {EXTRAS_FORMAT!r}")
    if extras.get("layoutId") not in (None, lid):
        problems.append(f"sidecar belongs to layout {extras.get('layoutId')}")
    for key, kind in EXTRAS_SHAPES.items():
        if not isinstance(extras.get(key, kind()), kind):
            problems.append(f"{key} must be a JSON {'object' if kind is dict else 'array'}")
    for key in ("width", "height"):
        if key in extras and not (_is_number(extras[key]) and extras[key] > 0):
            problems.append(f"{key} must be a positive number")
    if not isinstance(extras.get("twoWay", False), bool) or not isinstance(extras.get("site", ""), str):
        problems.append("twoWay must be a boolean and site a string")
    if problems:
        return problems

    node_ids = {n["nodeId"] for n in lay["nodes"]}
    edge_ends = {e["edgeId"]: (e["startNodeId"], e["endNodeId"]) for e in lay["edges"]}
    mapping = extras.get("vehicleTypes", {})
    if mapping:
        used = {p["vehicleTypeId"] for x in lay["nodes"] + lay["edges"]
                for p in x.get("vehicleTypeNodeProperties", x.get("vehicleTypeEdgeProperties", []))}
        problems += [f"vehicleTypes does not map vehicle type {t}" for t in sorted(used - set(mapping))]
        problems += [f"vehicleTypes maps {t} to a non-string class"
                     for t, c in mapping.items() if not isinstance(c, str)]
    problems += [f"nodeKinds names unknown node {n}" for n in extras.get("nodeKinds", {}) if n not in node_ids]
    problems += [f"nodeKinds gives node {n} a non-string kind"
                 for n, k in extras.get("nodeKinds", {}).items() if not isinstance(k, str)]
    problems += [f"noStop names unknown node {n}" for n in extras.get("noStop", []) if not _known(n, node_ids)]
    overrides = extras.get("edges", {})
    for eid, o in overrides.items():
        if eid not in edge_ends or not isinstance(o, dict):
            problems.append(f"edges: {eid} is not an edge of layout {lid} with an object of overrides")
            continue
        if "length" in o and not (_is_number(o["length"]) and o["length"] > 0):
            problems.append(f"edges: {eid} length must be a positive number")
        if not isinstance(o.get("oneway", False), bool) or not isinstance(o.get("lane", ""), str):
            problems.append(f"edges: {eid} oneway must be a boolean and lane a string")
        if "classes" in o and not (isinstance(o["classes"], list) and all(isinstance(c, str) for c in o["classes"])):
            problems.append(f"edges: {eid} classes must be an array of strings")
    if problems:
        return problems
    lanes = {overrides.get(eid, {}).get("lane", lane_key(*ends)) for eid, ends in edge_ends.items()}
    for d in extras.get("doors", []):
        if not isinstance(d, dict) or not isinstance(d.get("lanes", []), list):
            problems.append(f"doors: {d!r} is not a door object")
            continue
        problems += [f"door {d.get('id')} names unknown lane {ln}"
                     for ln in d.get("lanes", []) if not _known(ln, lanes)]
    return problems


def _not_applied(lay: dict) -> Counter:
    """LIF members present in the layout that a Layout cannot represent."""
    found: Counter = Counter()

    def required(prop: dict) -> int:
        return sum(1 for a in prop.get("actions") or [] if a.get("requirementType") == "REQUIRED")

    for n in lay["nodes"]:
        if n.get("mapId", MAP_ID) != MAP_ID:
            found[f"mapId {n['mapId']} (orders use {MAP_ID})"] += 1
        for p in n["vehicleTypeNodeProperties"]:
            found["node theta"] += "theta" in p
            found["REQUIRED node action"] += required(p)
    for e in lay["edges"]:
        for p in e["vehicleTypeEdgeProperties"]:
            found["rotationAllowed=true"] += p.get("rotationAllowed") is True
            for key in ("rotationAtStartNodeAllowed", "rotationAtEndNodeAllowed"):
                found[key] += p.get(key, "BOTH") != "BOTH"
            found["vehicleOrientation"] += (p.get("orientationType", "TANGENTIAL") != "TANGENTIAL"
                                           or p.get("vehicleOrientation", 0.0) != 0.0)
            for key in ("maxRotationSpeed", "minHeight", "maxHeight", "trajectory"):
                found[key] += key in p
            lr = p.get("loadRestriction")
            found["loadRestriction"] += lr is not None and not (
                isinstance(lr, dict) and lr.get("loaded") is True and lr.get("unloaded") is True
                and not lr.get("loadSetNames"))
            found["reentryAllowed=false"] += p.get("reentryAllowed", True) is not True
            found["REQUIRED edge action"] += required(p)
    return +found


# ---- import ------------------------------------------------------------------
def _pick_layout(doc: dict, layout_id: str | None) -> dict:
    layouts = doc["layouts"]
    if layout_id is None:
        if len(layouts) != 1:
            ids = [lay["layoutId"] for lay in layouts]
            raise LIFError(f"LIF holds {len(layouts)} layouts {ids}; pass layout_id")
        return layouts[0]
    for lay in layouts:
        if lay["layoutId"] == layout_id:
            return lay
    raise LIFError(f"LIF has no layout {layout_id}")


def import_lif(doc: dict, extras: dict | None = None, layout_id: str | None = None,
               *, default_speed: float | None = None, strict: bool = False) -> Layout:
    """Layout from one LIF layout and, optionally, its sidecar from ``export_site_extras``.

    Vehicle types: LIF lists are closed (a type without a property may not use the node or edge).
    With a sidecar, types map to agvClass and an edge open to every declared type becomes
    unrestricted; without one, each edge keeps exactly its vehicleTypeIds as classes.
    Differing per-type speeds collapse to the lowest. An edge with no maxSpeed at all ("no
    limitation") needs ``default_speed``, since planning divides by edge speed.
    Members a Layout cannot represent (orientation, rotation limits, heights, load restrictions,
    trajectories, REQUIRED actions, a mapId other than the one orders use, ...) are not applied:
    a LIFWarning names them, or with ``strict`` a LIFError (LIF 8.1.1 asks for one or the other).
    Without a sidecar the layout is routable but bare: no zones, racks, coverage or doors, and node
    kinds guessed from stations (charger with startCharging, other stations, waypoints).
    """
    if default_speed is not None and not (_is_number(default_speed) and default_speed > 0):
        raise LIFError("default_speed must be a positive number")
    problems = validate_lif(doc)
    if problems:
        raise LIFError(_summary("invalid LIF", problems))
    if extras is not None and not isinstance(extras, dict):
        raise LIFError("sidecar must be a JSON object")
    extras = extras or {}
    lay = _pick_layout(doc, layout_id)
    lid = lay["layoutId"]
    if extras:
        problems = _extras_problems(extras, lay)
        if problems:
            raise LIFError(_summary(f"sidecar does not fit layout {lid}", problems))
    ignored = _not_applied(lay)
    if ignored:
        msg = f"layout {lid}: LIF members not applied: " + ", ".join(f"{k} x{v}" for k, v in sorted(ignored.items()))
        if strict:
            raise LIFError(msg)
        warnings.warn(msg, LIFWarning, stacklevel=2)

    stations = {nid for st in lay.get("stations", []) for nid in st["interactionNodeIds"]}
    charging = {n["nodeId"] for n in lay["nodes"] for p in n["vehicleTypeNodeProperties"]
                for a in p.get("actions") or [] if a.get("actionType") == "startCharging"}
    kinds = extras.get("nodeKinds", {})
    nodes: dict[str, Node] = {}
    for n in lay["nodes"]:
        nid = n["nodeId"]
        guess = "charger" if nid in charging else "station" if nid in stations else "waypoint"
        nodes[nid] = Node(nid, float(n["nodePosition"]["x"]), float(n["nodePosition"]["y"]), kinds.get(nid, guess))

    classes_of = extras.get("vehicleTypes", {})
    declared = set(classes_of)
    pairs = {(e["startNodeId"], e["endNodeId"]) for e in lay["edges"]}
    overrides = extras.get("edges", {})
    edges: dict[str, Edge] = {}
    for e in lay["edges"]:
        eid, s, t = e["edgeId"], e["startNodeId"], e["endNodeId"]
        if t not in nodes:
            raise LIFError(f"edge {eid} leads to node {t} in another layout; a Layout is a single plane")
        props = e["vehicleTypeEdgeProperties"]
        speeds = [p["maxSpeed"] for p in props if p.get("maxSpeed") is not None]
        if not all(_is_number(v) and v > 0 for v in speeds):
            raise LIFError(f"edge {eid}: maxSpeed must be a positive number")
        if not speeds:
            if default_speed is None:
                raise LIFError(f"edge {eid} has no maxSpeed (no limitation); pass default_speed")
            speeds = [default_speed]
        types = {p["vehicleTypeId"] for p in props}
        o = overrides.get(eid, {})
        if "classes" in o:
            classes = frozenset(o["classes"])
        elif declared and types >= declared:
            classes = frozenset()
        else:
            classes = frozenset(classes_of.get(v, v) for v in types)
        edges[eid] = Edge(eid, s, t, o.get("length", _length(nodes, s, t)), float(min(speeds)),
                          o.get("lane", lane_key(s, t)), o.get("oneway", (t, s) not in pairs), classes)

    try:
        zones = {z["id"]: Zone(z["id"], z["name"], z["kind"], _tuples(z["polygon"]), z.get("staticSpeed"),
                               z.get("humanSpeed"), bool(z.get("locked", False)),
                               collaborative=bool(z.get("collaborative", False))) for z in extras.get("zones", [])}
        doors = {d["id"]: DoorSpec(d["id"], d["name"], tuple(d["a"]), tuple(d["b"]), frozenset(d.get("lanes", [])))
                 for d in extras.get("doors", [])}
        racks = [_tuples(r) for r in extras.get("racks", [])]
        coverage = {k: _tuples(v) for k, v in extras.get("coverage", {}).items()}
        walls = [_tuples(w) for w in extras.get("walls", [])]
    except (KeyError, TypeError, IndexError, ValueError) as ex:
        raise LIFError(f"sidecar for layout {lid} is malformed: {ex!r}") from ex
    return Layout(nodes, edges, racks, zones, coverage,
                  width=extras.get("width", max((n.x for n in nodes.values()), default=0.0)),
                  height=extras.get("height", max((n.y for n in nodes.values()), default=0.0)),
                  two_way=extras.get("twoWay", bool(edges) and all(not e.oneway for e in edges.values())),
                  site=extras.get("site", lid), doors=doors, walls=walls, no_stop=set(extras.get("noStop", [])))
