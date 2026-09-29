"""Time-based A* on the directed roadmap.

Cost is travel time: edge length / edge max speed plus a stop-and-turn penalty
when the heading changes, because a differential-drive AMR stops to rotate.
Dynamic penalties (congestion, blocked resources) are passed per query.
"""
from __future__ import annotations

import heapq
import math

from .geometry import wrap_angle
from .layout import Layout

TURN_RATE = 1.6
TURN_OVERHEAD_S = 1.2
# dead-end stubs (stations, bays): only ever entered as the goal of a route
STUB_KINDS = {"inbound", "outbound", "charger", "park", "in_stage", "out_stage", "pack"}


class Planner:
    def __init__(self, layout: Layout, vmax: float = 1.5) -> None:
        self.layout = layout
        self.vmax = vmax
        self._static: dict[str, dict[str, float]] = {}
        self._static_m: dict[str, dict[str, float]] = {}

    def _heading(self, a: str, b: str) -> float:
        na, nb = self.layout.nodes[a], self.layout.nodes[b]
        return math.atan2(nb.y - na.y, nb.x - na.x)

    def turn_cost(self, prev: str | None, cur: str, nxt: str) -> float:
        if prev is None:
            return 0.0
        d = abs(wrap_angle(self._heading(cur, nxt) - self._heading(prev, cur)))
        return 0.0 if d < 0.3 else d / TURN_RATE + TURN_OVERHEAD_S

    def plan(self, start: str, goal: str, avoid_nodes: set[str] | frozenset = frozenset(),
             avoid_lanes: set[str] | frozenset = frozenset(), node_penalty: dict[str, float] | None = None,
             prev: str | None = None, vclass: str | None = None) -> list[str] | None:
        """Shortest-time path start..goal, or None. ``avoid_*`` are hard exclusions."""
        if start == goal:
            return [start]
        if goal in avoid_nodes:
            return None
        penalty = node_penalty or {}
        g_node = self.layout.nodes[goal]

        def h(n: str) -> float:
            nn = self.layout.nodes[n]
            return math.hypot(nn.x - g_node.x, nn.y - g_node.y) / self.vmax

        start_state = (start, prev)
        best: dict[tuple[str, str | None], float] = {start_state: 0.0}
        parent: dict[tuple[str, str | None], tuple[str, str | None] | None] = {start_state: None}
        heap = [(h(start), 0.0, start, prev)]
        counter = 0
        while heap:
            f, g, cur, p = heapq.heappop(heap)
            state = (cur, p)
            if g > best.get(state, math.inf) + 1e-9:
                continue
            if cur == goal:
                path = [cur]
                s = parent[state]
                while s is not None:
                    path.append(s[0])
                    s = parent[s]
                return path[::-1]
            for e in self.layout.out_edges[cur]:
                nxt = e.end
                if nxt in avoid_nodes or e.lane in avoid_lanes or not self.layout.allowed(e, vclass):
                    continue
                # stubs (stations, chargers, parks) are only entered as the goal
                if self.layout.nodes[nxt].kind in STUB_KINDS and nxt != goal:
                    continue
                cost = g + e.length / e.max_speed + self.turn_cost(p, cur, nxt) + penalty.get(nxt, 0.0)
                ns = (nxt, cur)
                if cost < best.get(ns, math.inf) - 1e-9:
                    best[ns] = cost
                    parent[ns] = state
                    counter += 1
                    heapq.heappush(heap, (cost + h(nxt), cost, nxt, cur))
        return None

    def _dijkstra(self, source: str, vclass: str | None = None) -> tuple[dict[str, float], dict[str, float]]:
        dist_t = {source: 0.0}
        dist_m = {source: 0.0}
        heap = [(0.0, source)]
        while heap:
            d, cur = heapq.heappop(heap)
            if d > dist_t.get(cur, math.inf):
                continue
            if cur != source and self.layout.nodes[cur].kind in STUB_KINDS:
                continue   # never drive through a dead end
            for e in self.layout.out_edges[cur]:
                if not self.layout.allowed(e, vclass):
                    continue
                nd = d + e.length / e.max_speed
                if nd < dist_t.get(e.end, math.inf):
                    dist_t[e.end] = nd
                    dist_m[e.end] = dist_m[cur] + e.length
                    heapq.heappush(heap, (nd, e.end))
        return dist_t, dist_m

    def _tables(self, a: str, vclass: str | None):
        key = (a, vclass)
        if key not in self._static:
            self._static[key], self._static_m[key] = self._dijkstra(a, vclass)
        return self._static[key], self._static_m[key]

    def travel_time(self, a: str, b: str, vclass: str | None = None) -> float:
        return self._tables(a, vclass)[0].get(b, math.inf)

    def distance(self, a: str, b: str, vclass: str | None = None) -> float:
        return self._tables(a, vclass)[1].get(b, math.inf)

    def route_length(self, route: list[str]) -> float:
        return sum(self.layout.edge_between(a, b).length for a, b in zip(route, route[1:]))
