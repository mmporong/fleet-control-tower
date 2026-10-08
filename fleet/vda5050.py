"""VDA 5050 v2.0 message shapes used between master control and vehicles.

Only the subset this system exercises is modelled: order (with base/horizon),
instantActions, state, connection and factsheet-free visualization. Field names
follow the specification so recorded traffic can be compared with real AGVs.

Custom content is kept in fields prefixed with ``x_`` (for example onboard
person detections), because the standard defines no slot for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any

from .clock import Clock, SimClock

VERSION = "2.0.0"
INTERFACE = "uagv"
MAJOR = "v2"
MAP_ID = "warehouse-a"

OPERATING_MODES = {"AUTOMATIC", "SEMIAUTOMATIC", "MANUAL", "SERVICE", "TEACHIN"}
ACTION_STATUSES = {"WAITING", "INITIALIZING", "RUNNING", "PAUSED", "FINISHED", "FAILED"}
BLOCKING_TYPES = {"NONE", "SOFT", "HARD"}
ERROR_LEVELS = {"WARNING", "FATAL"}
CONNECTION_STATES = {"ONLINE", "OFFLINE", "CONNECTIONBROKEN"}
# actions this simulated vehicle declares support for
NODE_ACTIONS = {"pick", "drop", "startCharging", "stopCharging",
                # custom node actions for collaborative tote picking (declared in the factsheet)
                "x_toteInduct", "x_toteDrop", "x_pickItems"}
INSTANT_ACTIONS = {"startPause", "stopPause", "cancelOrder", "stateRequest", "initPosition", "factsheetRequest",
                   # custom actions, declared in this vehicle's factsheet
                   "x_fleetSpeedLimit", "x_resetFault", "x_fieldServiceDone", "x_remoteStop",
                   "x_setOperatingMode", "x_teleop"}


def topic(manufacturer: str, serial: str, name: str) -> str:
    return f"{INTERFACE}/{MAJOR}/{manufacturer}/{serial}/{name}"


def split_topic(value: str) -> tuple[str, str, str] | None:
    parts = value.split("/")
    if len(parts) != 5 or parts[0] != INTERFACE or parts[1] != MAJOR:
        return None
    return parts[2], parts[3], parts[4]


class HeaderIds:
    """headerId is defined per topic and increases by one per message."""

    def __init__(self) -> None:
        self._counters: dict[str, count] = {}

    def next(self, topic_name: str) -> int:
        if topic_name not in self._counters:
            self._counters[topic_name] = count(0)
        return next(self._counters[topic_name])


_SIM_CLOCK = SimClock()


def iso_time(sim_time_s: float) -> str:
    """Simulation clock as an ISO-8601 string on a fixed shift start date."""
    return _SIM_CLOCK.iso(sim_time_s)


def parse_iso_time(stamp: str) -> float:
    """Inverse of :func:`iso_time`.

    >>> parse_iso_time(iso_time(3723.456))
    3723.456
    >>> parse_iso_time(iso_time(90000.0))    # second day of the shift calendar
    90000.0
    """
    t = _SIM_CLOCK.parse(stamp)
    if t is None:
        raise ValueError(f"not an ISO-8601 timestamp: {stamp!r}")
    return round(t, 3)


def header(ids: HeaderIds, topic_name: str, manufacturer: str, serial: str, t: float,
           clock: Clock | None = None) -> dict:
    """Common header; ``t`` is seconds on ``clock`` (simulation calendar by default)."""
    return {
        "headerId": ids.next(topic_name),
        "timestamp": (clock or _SIM_CLOCK).iso(t),
        "version": VERSION,
        "manufacturer": manufacturer,
        "serialNumber": serial,
    }


def action(action_type: str, action_id: str, blocking: str = "HARD",
           params: dict[str, Any] | None = None) -> dict:
    if blocking not in BLOCKING_TYPES:
        raise ValueError(f"blockingType {blocking!r} is not defined by VDA 5050")
    return {
        "actionType": action_type,
        "actionId": action_id,
        "blockingType": blocking,
        "actionParameters": [{"key": k, "value": v} for k, v in (params or {}).items()],
    }


@dataclass
class RouteNode:
    node_id: str
    x: float
    y: float
    actions: list[dict] = field(default_factory=list)
    theta: float | None = None        # required heading at the node (stations), with allowedDeviationTheta


@dataclass
class RouteEdge:
    edge_id: str
    start: str
    end: str
    max_speed: float


def build_order(hdr: dict, order_id: str, update_id: int, nodes: list[RouteNode],
                edges: list[RouteEdge], first_sequence_id: int, released_nodes: int,
                zone_set_id: str = MAP_ID) -> dict:
    """Build an order whose first ``released_nodes`` nodes form the base.

    Nodes take even sequenceIds and edges odd ones, continuing from
    ``first_sequence_id`` (the sequenceId of ``nodes[0]``). An edge is released
    when its end node is released.
    """
    if len(edges) != len(nodes) - 1:
        raise ValueError("an order needs exactly one edge between consecutive nodes")
    if first_sequence_id % 2:
        raise ValueError("node sequenceIds are even")
    if not 1 <= released_nodes <= len(nodes):
        raise ValueError("the base must contain at least the first node")
    out_nodes = []
    for i, n in enumerate(nodes):
        pos = {"x": round(n.x, 3), "y": round(n.y, 3), "mapId": MAP_ID, "allowedDeviationXY": 0.5}
        if n.theta is not None:
            pos["theta"] = round(n.theta, 3)
            pos["allowedDeviationTheta"] = 0.1
        out_nodes.append({
            "nodeId": n.node_id,
            "sequenceId": first_sequence_id + 2 * i,
            "released": i < released_nodes,
            "nodePosition": pos,
            "actions": list(n.actions),
        })
    out_edges = []
    for i, e in enumerate(edges):
        if e.start != nodes[i].node_id or e.end != nodes[i + 1].node_id:
            raise ValueError(f"edge {e.edge_id} does not join {nodes[i].node_id}->{nodes[i + 1].node_id}")
        out_edges.append({
            "edgeId": e.edge_id,
            "sequenceId": first_sequence_id + 2 * i + 1,
            "released": i + 1 < released_nodes,
            "startNodeId": e.start,
            "endNodeId": e.end,
            "maxSpeed": e.max_speed,
            "actions": [],
        })
    return {**hdr, "orderId": order_id, "orderUpdateId": update_id, "zoneSetId": zone_set_id,
            "nodes": out_nodes, "edges": out_edges}


def validate_order(msg: dict) -> list[str]:
    """Structural checks from the specification that do not need vehicle state."""
    problems: list[str] = []
    for key in ("headerId", "timestamp", "version", "manufacturer", "serialNumber",
                "orderId", "orderUpdateId", "nodes", "edges"):
        if key not in msg:
            problems.append(f"missing {key}")
    if problems:
        return problems
    nodes, edges = msg["nodes"], msg["edges"]
    if not nodes:
        return ["an order needs at least one node"]
    if len(edges) != len(nodes) - 1:
        problems.append("edge count must be node count - 1")
    seq = [n["sequenceId"] for n in nodes]
    if any(s % 2 for s in seq):
        problems.append("node sequenceIds must be even")
    if any(b - a != 2 for a, b in zip(seq, seq[1:])):
        problems.append("node sequenceIds must increase by 2")
    released = [n["released"] for n in nodes]
    if not released[0]:
        problems.append("first node must be released")
    if any((not a) and b for a, b in zip(released, released[1:])):
        problems.append("released nodes must form a prefix (base before horizon)")
    for i, e in enumerate(edges[: len(nodes) - 1]):
        if e["sequenceId"] != seq[i] + 1:
            problems.append(f"edge {e['edgeId']} sequenceId must sit between its nodes")
        if e["startNodeId"] != nodes[i]["nodeId"] or e["endNodeId"] != nodes[i + 1]["nodeId"]:
            problems.append(f"edge {e['edgeId']} does not connect consecutive nodes")
        if e["released"] and not nodes[i + 1]["released"]:
            problems.append(f"edge {e['edgeId']} released beyond the base")
        if e["released"] != nodes[i + 1]["released"]:
            problems.append(f"edge {e['edgeId']} release flag must match its end node")
    for n in nodes:
        for a in n.get("actions", []):
            if a.get("blockingType") not in BLOCKING_TYPES:
                problems.append(f"action {a.get('actionId')} has invalid blockingType")
    return problems


def build_instant_actions(hdr: dict, actions: list[dict]) -> dict:
    return {**hdr, "actions": actions}


def build_connection(hdr: dict, state: str) -> dict:
    if state not in CONNECTION_STATES:
        raise ValueError(state)
    return {**hdr, "connectionState": state}


def base_end(msg_nodes: list[dict]) -> dict:
    """Last released node of an order's node list."""
    released = [n for n in msg_nodes if n["released"]]
    return released[-1]
