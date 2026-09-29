"""Small hand-built worlds for deterministic traffic tests."""
from __future__ import annotations

import random

from fleet.bus import Bus
from fleet.control import FleetManager, ManagerConfig
from fleet.layout import Layout, warehouse_layout
from fleet.perception import Detection
from fleet.vda5050 import topic
from fleet.vehicle import Obstacle, Vehicle


class MiniWorld:
    """Vehicles + bus + manager without people, tasks or random events."""

    def __init__(self, placements: dict[str, str], two_way: bool = False, strategy: str = "locks+resolve",
                 layout: Layout | None = None, config: ManagerConfig | None = None) -> None:
        self.layout = layout or warehouse_layout(two_way)
        self.bus = Bus(random.Random(1))
        cfg = config or ManagerConfig()
        cfg.strategy = strategy
        self.manager = FleetManager(self.layout, self.bus, cfg)
        self.vehicles: dict[str, Vehicle] = {}
        self.now = 0.0
        self.extra_obstacles: list[Obstacle] = []
        self.detections: list[Detection] = []
        self.feed_active = False          # set when a test drives the infrastructure feed
        for serial, node in placements.items():
            v = Vehicle(serial, self.layout, node, random.Random(hash(serial) % 1000), battery=90.0)
            v.perf = 1.0
            self.vehicles[serial] = v
            self.manager.register(serial)
            self.bus.subscribe(topic("sim", serial, "order"), lambda t, m, v=v: v.on_order(m))
            self.bus.subscribe(topic("sim", serial, "instantActions"),
                               lambda t, m, v=v: v.on_instant_actions(m, self.now))
            self.bus.link(serial).latency_s = 0.02
            self.bus.link(serial).jitter_s = 0.0
        self._tick = 0.0
        self.external: tuple | None = None   # (id, x, y, theta, itinerary) reported every tick when set

    def step(self, dt: float = 0.1) -> None:
        self.now = round(self.now + dt, 6)
        obstacles = [Obstacle(v.serial, "amr", v.x, v.y, v.spec.radius) for v in self.vehicles.values()]
        obstacles += self.extra_obstacles
        for v in self.vehicles.values():
            v.step(dt, self.now, obstacles)
            for t, msg in v.publish_due(self.now):
                self.bus.publish(t, msg, self.now)
        self.bus.deliver_due(self.now)
        if self.now >= self._tick - 1e-9:
            self._tick = self.now + 0.2
            if self.feed_active or self.detections:
                self.feed_active = True
                self.manager.ingest_detections(self.detections)
            if self.external:
                self.manager.ingest_external(*self.external, speed=0.5)
            self.manager.tick(self.now, 0.2)

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.step()

    def run_until(self, cond, limit: float) -> bool:
        end = self.now + limit
        while self.now < end:
            self.step()
            if cond():
                return True
        return False

    def send(self, serial: str, goal: str, purpose: str = "test") -> None:
        rec = self.manager.robots[serial]
        assert self.manager.assign_route(rec, goal, purpose), f"no route {rec.last_node}->{goal}"

    def at(self, serial: str, node: str) -> bool:
        v = self.vehicles[serial]
        return v.last_node_id == node and v.progress == 0.0 and abs(v.v) < 1e-6
