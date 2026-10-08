"""Vehicle simulator over MQTT: VDA 5050 vehicles as separate network clients.

Stands in for a vendor's vehicle simulator or the vehicles themselves when the
fleet manager runs as a service (``fleet.fms``). Each vehicle

- is its own MQTT client with a Last Will on ``uagv/v2/<m>/<serial>/connection``
  (CONNECTIONBROKEN, retained), publishes ONLINE (retained) on every connect and
  OFFLINE before a clean shutdown;
- runs the same vehicle model as the simulation (``fleet.vehicle``), in real
  time, with its own clock for message timestamps;
- sees only the other simulated vehicles as obstacles (no people here).

With ``--doors`` it also stands in for the site's door controllers: one more
MQTT client runs a ``facility.DoorDevice`` per door of the layout (open on
request, presence sensor on the simulated vehicles, curtain as an obstacle).
Without it a site with doors has no door answering, and the fleet manager
keeps every door out of its routes, as it should.

    python3 -m fleet.agvsim --broker 127.0.0.1:1883 --site sites/dc.lif.json --doors \\
        --vehicle AGF-01:FORKLIFT:PA14_3 --vehicle AGF-02:FORKLIFT:PA19_9
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
import random
import signal
import threading
import traceback

from .clock import WallClock
from .facility import DoorDevice
from .layout import Layout
from .mqtt import MqttClient, Will
from .mqttbus import MqttBus, add_broker_args, broker_options
from .vda5050 import topic
from .vehicle import SPEC_GRID, Obstacle, Vehicle, VehicleSpec, agf_spec, tote_amr_spec

log = logging.getLogger(__name__)


def spec_for(vclass: str) -> VehicleSpec:
    return {"FORKLIFT": agf_spec, "CARRIER": tote_amr_spec}.get(vclass, lambda: SPEC_GRID)()


class SimVehicleClient:
    """One simulated vehicle and its MQTT connection."""

    def __init__(self, vehicle: Vehicle, host: str, port: int, clock: WallClock, keepalive: int = 15,
                 tls=None, username: str | None = None, password: str | None = None) -> None:
        self.v = vehicle
        self.clock = clock
        vehicle.clock = clock
        self.conn_topic = topic(vehicle.manufacturer, vehicle.serial, "connection")
        # the Will's header is fixed at connect time; VDA 5050 notes its timestamp is therefore stale
        will = json.dumps(vehicle.connection_msg(clock.now(), "CONNECTIONBROKEN"), separators=(",", ":"))
        self.client = MqttClient(host, port, f"{vehicle.manufacturer}-{vehicle.serial}", keepalive=keepalive,
                                 tls=tls, username=username, password=password,
                                 will=Will(self.conn_topic, will.encode("utf-8"), 1, True))
        self.bus = MqttBus(self.client, clock)
        self.bus.subscribe(topic(vehicle.manufacturer, vehicle.serial, "order"), lambda t, m: vehicle.on_order(m))
        self.bus.subscribe(topic(vehicle.manufacturer, vehicle.serial, "instantActions"),
                           lambda t, m: vehicle.on_instant_actions(m, clock.now()))
        self.client.on_connect = self._online

    def _online(self) -> None:
        # runs on the network thread: queue-free publish of the retained ONLINE state
        self.bus.publish(self.conn_topic, self.v.connection_msg(self.clock.now(), "ONLINE"), self.clock.now())
        self.v._factsheet_due = True          # a manager that just subscribed gets the factsheet again

    def start(self) -> None:
        self.client.start()

    def step(self, dt: float, now: float, obstacles: list[Obstacle]) -> None:
        self.bus.deliver_due(now)
        self.v.step(dt, now, obstacles)
        if self.client.connected:
            for t, msg in self.v.publish_due(now):
                self.bus.publish(t, msg, now)

    def stop(self, clean: bool = True) -> None:
        """Clean: OFFLINE then DISCONNECT (no Will). Not clean: drop the socket like a crash (Will goes out)."""
        if clean and self.client.connected:
            self.bus.publish(self.conn_topic, self.v.connection_msg(self.clock.now(), "OFFLINE"), self.clock.now())
            self.client.disconnect()
        self.client.stop()


class AgvSim:
    """Several simulated vehicles stepped together in real time (they see each other as obstacles)."""

    def __init__(self, layout: Layout, vehicles: list[tuple[str, str, str]], host: str, port: int,
                 clock: WallClock | None = None, manufacturer: str = "sim", dt: float = 0.1, seed: int = 1,
                 tls=None, doors: bool = False, username: str | None = None, password: str | None = None) -> None:
        self.layout = layout
        self.clock = clock or WallClock()
        self.dt = dt
        self.clients: dict[str, SimVehicleClient] = {}
        for i, (serial, vclass, node) in enumerate(vehicles):
            if node not in layout.nodes:
                raise ValueError(f"{serial}: unknown start node {node}")
            v = Vehicle(serial, layout, node, random.Random(seed * 1000 + i), manufacturer=manufacturer,
                        battery=90.0, spec=spec_for(vclass))
            out = [e for e in layout.out_edges[node] if layout.allowed(e, v.spec.vclass)]
            if out:
                n0, n1 = layout.nodes[node], layout.nodes[out[0].end]
                v.theta = math.atan2(n1.y - n0.y, n1.x - n0.x)
            self.clients[serial] = SimVehicleClient(v, host, port, self.clock, tls=tls, username=username,
                                                    password=password)
        # door controllers: their own MQTT client, the way a door PLC gateway is its own device
        self.door_client = self.door_bus = None
        self.doors: list[DoorDevice] = []
        if doors and layout.doors:
            self.door_client = MqttClient(host, port, f"{manufacturer}-doors", keepalive=15, tls=tls,
                                          username=username, password=password)
            self.door_bus = MqttBus(self.door_client, self.clock)
            self.doors = [DoorDevice(spec, self.door_bus) for spec in layout.doors.values()]
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for c in self.clients.values():
            c.start()
        if self.door_client is not None:
            self.door_client.start()
        self._thread = threading.Thread(target=self._run, name="agvsim", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = self.clock.now()
        while not self._halt.wait(self.dt / max(1e-6, self.clock.scale)):
            now = self.clock.now()
            dt = min(0.5, max(0.0, now - last))
            last = now
            try:
                self._step(dt, now)
            except Exception:                 # noqa: BLE001 - one bad message must not freeze every vehicle
                log.error("vehicle simulator step failed:\n%s", traceback.format_exc())

    def _step(self, dt: float, now: float) -> None:
        bodies = {s: c.v.spec.circles(c.v.x, c.v.y, c.v.theta) for s, c in self.clients.items()}
        curtains = []
        if self.door_bus is not None:
            self.door_bus.deliver_due(now)
            all_bodies = [b for circles in bodies.values() for b in circles]
            for d in self.doors:
                d.step(dt, now, all_bodies)
                if self.door_client.connected:
                    d.publish_due(now)
                curtains += [Obstacle(d.spec.id, "door", x, y, r) for x, y, r in d.obstacles()]
        for serial, c in list(self.clients.items()):
            obstacles = [Obstacle(o, "amr", x, y, r) for o, circles in bodies.items() if o != serial
                         for x, y, r in circles]
            c.step(dt, now, obstacles + curtains)

    def crash(self, serial: str) -> None:
        """Test hook: the vehicle disappears without a clean disconnect."""
        c = self.clients.pop(serial)
        c.stop(clean=False)

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(2.0)
        for c in self.clients.values():
            c.stop(clean=True)
        if self.door_client is not None:
            if self.door_client.connected:
                self.door_client.disconnect()
            self.door_client.stop()


def main(argv: list[str] | None = None) -> int:
    from .lif import import_lif
    ap = argparse.ArgumentParser(description="VDA 5050 차량 시뮬레이터(MQTT)")
    add_broker_args(ap)
    ap.add_argument("--site", required=True, help="VDMA LIF 파일 (예: sites/dc.lif.json)")
    ap.add_argument("--extras", help="사이드카(기본: <site>에서 .lif.json → .site.json)")
    ap.add_argument("--vehicle", action="append", required=True, metavar="SERIAL:CLASS:NODE",
                    help="예: AGF-01:FORKLIFT:PA14_3 (CLASS: FORKLIFT|CARRIER|GENERIC)")
    ap.add_argument("--manufacturer", default="sim")
    ap.add_argument("--doors", action="store_true", help="현장 고속문 제어기도 모의(레이아웃의 문마다 MQTT로 응답)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    b = broker_options(args)
    site = Path(args.site)
    extras_path = Path(args.extras) if args.extras else site.with_name(site.name.replace(".lif.json", ".site.json"))
    layout = import_lif(json.loads(site.read_text()), json.loads(extras_path.read_text()))
    specs = [tuple(v.split(":", 2)) for v in args.vehicle]
    sim = AgvSim(layout, specs, b["host"], b["port"], manufacturer=args.manufacturer, doors=args.doors, tls=b["tls"],
                 username=b["username"], password=b["password"])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    sim.start()
    print(f"차량 시뮬레이터: {len(specs)}대{f' · 고속문 {len(sim.doors)}개' if sim.doors else ''} → {args.broker}",
          flush=True)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
