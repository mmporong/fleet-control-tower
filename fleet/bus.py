"""In-process message bus with MQTT-style topics and simulated link quality.

VDA 5050 runs over MQTT. No broker is required here: this bus keeps the same
topic names, applies per-vehicle latency, loss and outages, and delivers in
simulation time so runs are reproducible from a seed. Swapping in a real MQTT
client only needs ``publish`` and ``subscribe`` with the same signatures.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import heapq
import json
import random
from typing import Callable

from .vda5050 import split_topic

Handler = Callable[[str, dict], None]


def topic_matches(pattern: str, value: str) -> bool:
    """MQTT wildcard match.

    >>> topic_matches("uagv/v2/+/+/state", "uagv/v2/sim/AMR-01/state")
    True
    >>> topic_matches("uagv/v2/#", "uagv/v2/sim/AMR-01/order")
    True
    >>> topic_matches("uagv/v2/+/AMR-02/state", "uagv/v2/sim/AMR-01/state")
    False
    """
    p_parts = pattern.split("/")
    v_parts = value.split("/")
    for i, p in enumerate(p_parts):
        if p == "#":
            return True
        if i >= len(v_parts):
            return False
        if p != "+" and p != v_parts[i]:
            return False
    return len(p_parts) == len(v_parts)


@dataclass
class LinkQuality:
    latency_s: float = 0.05
    jitter_s: float = 0.03
    loss: float = 0.0
    down_until: float = -1.0   # simulation time; messages are dropped until then


@dataclass(order=True)
class _Pending:
    due: float
    seq: int
    topic: str = field(compare=False)
    payload: dict = field(compare=False)


class Bus:
    def __init__(self, rng: random.Random, log_depth: int = 30) -> None:
        self._rng = rng
        self._subs: list[tuple[str, Handler]] = []
        self._queue: list[_Pending] = []
        self._seq = 0
        self.links: dict[str, LinkQuality] = {}
        self.log: dict[str, deque] = {}
        self._log_depth = log_depth
        self.stats = {"published": 0, "delivered": 0, "dropped": 0, "bytes": 0}

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    def link(self, serial: str) -> LinkQuality:
        if serial not in self.links:
            self.links[serial] = LinkQuality()
        return self.links[serial]

    def publish(self, topic: str, payload: dict, now: float) -> bool:
        """Queue a message; returns False when the link dropped it."""
        self.stats["published"] += 1
        parsed = split_topic(topic)
        serial = parsed[1] if parsed else None
        delay = 0.0
        if serial is not None:
            q = self.link(serial)
            if now < q.down_until or (q.loss > 0 and self._rng.random() < q.loss):
                self.stats["dropped"] += 1
                self._record(serial, topic, payload, now, dropped=True)
                return False
            delay = max(0.0, q.latency_s + self._rng.uniform(-q.jitter_s, q.jitter_s))
            self._record(serial, topic, payload, now, dropped=False)
        self._seq += 1
        heapq.heappush(self._queue, _Pending(now + delay, self._seq, topic, payload))
        return True

    def deliver_due(self, now: float) -> int:
        delivered = 0
        while self._queue and self._queue[0].due <= now:
            item = heapq.heappop(self._queue)
            for pattern, handler in self._subs:
                if topic_matches(pattern, item.topic):
                    handler(item.topic, item.payload)
            delivered += 1
        self.stats["delivered"] += delivered
        return delivered

    def _record(self, serial: str, topic: str, payload: dict, now: float, dropped: bool) -> None:
        name = topic.rsplit("/", 1)[-1]
        if name == "visualization":
            return
        if serial not in self.log:
            self.log[serial] = deque(maxlen=self._log_depth)
        size = len(json.dumps(payload, separators=(",", ":")))
        self.stats["bytes"] += size
        self.log[serial].append({"t": round(now, 2), "topic": topic, "dropped": dropped,
                                 "bytes": size, "payload": payload})

    def recent(self, serial: str, limit: int = 12) -> list[dict]:
        return list(self.log.get(serial, ()))[-limit:]
