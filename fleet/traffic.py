"""Reservation table and deadlock bookkeeping for the fleet manager.

Resources are roadmap nodes (``N:<node>``) and undirected lanes
(``L:<a>|<b>``). A lane lock covers both directions, so two robots never meet
head-on inside one lane. A vehicle may only drive what the manager released,
and the manager only releases what it has reserved for that vehicle.

Waiting robots form a wait-for graph (robot -> holder of the resource it
needs). Every robot waits for at most one holder, so the graph is functional
and a deadlock is simply a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass


def node_res(node_id: str) -> str:
    return f"N:{node_id}"


def lane_res(lane_key: str) -> str:
    return f"L:{lane_key}"


def res_node(resource: str) -> str | None:
    return resource[2:] if resource.startswith("N:") else None


class LockTable:
    def __init__(self) -> None:
        self.holder: dict[str, str] = {}
        self.held: dict[str, list[str]] = {}

    def holder_of(self, resource: str) -> str | None:
        return self.holder.get(resource)

    def resources_of(self, owner: str) -> list[str]:
        return list(self.held.get(owner, ()))

    def try_acquire(self, owner: str, resources: list[str]) -> tuple[bool, str | None, str | None]:
        """All-or-nothing acquisition. Returns (ok, blocking holder, blocking resource)."""
        for r in resources:
            h = self.holder.get(r)
            if h is not None and h != owner:
                return False, h, r
        for r in resources:
            if self.holder.get(r) != owner:
                self.holder[r] = owner
                self.held.setdefault(owner, []).append(r)
        return True, None, None

    def force(self, owner: str, resource: str) -> None:
        """Record occupancy that is a physical fact (robot is already there)."""
        prev = self.holder.get(resource)
        if prev == owner:
            return
        if prev is not None:
            self.held[prev].remove(resource)
        self.holder[resource] = owner
        self.held.setdefault(owner, []).append(resource)

    def release(self, owner: str, resources) -> None:
        mine = self.held.get(owner, [])
        for r in list(resources):
            if self.holder.get(r) == owner:
                del self.holder[r]
                mine.remove(r)

    def release_all(self, owner: str) -> None:
        self.release(owner, list(self.held.get(owner, [])))

    def retain_only(self, owner: str, keep: set[str]) -> list[str]:
        drop = [r for r in self.held.get(owner, []) if r not in keep]
        self.release(owner, drop)
        return drop

    def check(self) -> list[str]:
        """Internal consistency: the two indexes must describe the same table."""
        problems = []
        seen: dict[str, str] = {}
        for owner, lst in self.held.items():
            if len(lst) != len(set(lst)):
                problems.append(f"{owner} holds a resource twice")
            for r in lst:
                if r in seen:
                    problems.append(f"{r} held by {seen[r]} and {owner}")
                seen[r] = owner
                if self.holder.get(r) != owner:
                    problems.append(f"{r} index says {self.holder.get(r)}, list says {owner}")
        for r, owner in self.holder.items():
            if seen.get(r) != owner:
                problems.append(f"{r} missing from {owner}'s list")
        return problems


def find_cycles(wait_for: dict[str, str]) -> list[list[str]]:
    """Cycles in a functional graph (each robot waits for one holder).

    >>> find_cycles({"A": "B", "B": "A", "C": "A"})
    [['A', 'B']]
    >>> find_cycles({"A": "B", "B": "C"})
    []
    """
    color: dict[str, int] = {}
    cycles: list[list[str]] = []
    for start in sorted(wait_for):
        if start in color:
            continue
        path: list[str] = []
        index: dict[str, int] = {}
        cur: str | None = start
        while cur is not None and cur not in color:
            color[cur] = 1
            index[cur] = len(path)
            path.append(cur)
            cur = wait_for.get(cur)
        if cur is not None and color.get(cur) == 1 and cur in index:
            cycle = path[index[cur]:]
            k = cycle.index(min(cycle))
            cycles.append(cycle[k:] + cycle[:k])
        for n in path:
            color[n] = 2
    return cycles


@dataclass
class WaitInfo:
    since: float
    kind: str            # robot | human | forklift | zone | external | offline | fault
    resource: str
    holder: str | None
    detail: str = ""
