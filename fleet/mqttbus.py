"""The fleet manager's bus over a real MQTT broker (runtime), with the interface of ``fleet.bus.Bus``.

``FleetManager``, ``DoorSupervisor`` and the vehicle simulator only call
``subscribe``, ``publish`` and ``deliver_due`` and read ``now``, so the same
control code runs in the deterministic simulation and against a broker:

- messages are JSON over MQTT; ``connection`` goes out at QoS 1 and retained
  (VDA 5050: the broker keeps each vehicle's last connection state, the Last Will
  sets it to CONNECTIONBROKEN), everything else at QoS 0;
- MQTT callbacks run on the client's network thread; they only queue the
  message. ``deliver_due`` hands queued messages to the handlers on the caller's
  thread, so the manager stays single-threaded as in the simulation;
- a payload that is not a JSON object is counted and dropped here; schema
  checks are the manager's ``validator``.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
import queue
import ssl
from typing import Callable

from .bus import LinkQuality
from .clock import WallClock
from .mqtt import MqttClient, topic_matches
from .vda5050 import split_topic

Handler = Callable[[str, dict], None]


def add_broker_args(ap: argparse.ArgumentParser) -> None:
    """Broker options shared by the services. The password only comes from the environment (not ``ps``)."""
    ap.add_argument("--broker", default="127.0.0.1:1883", help="브로커 호스트:포트(TLS 기본 포트 8883)")
    ap.add_argument("--mqtt-user", help="브로커 사용자 이름. 비밀번호는 환경변수 FCT_MQTT_PASSWORD")
    ap.add_argument("--tls", action="store_true", help="TLS로 연결(시스템 신뢰 저장소)")
    ap.add_argument("--cafile", help="브로커 CA 인증서(지정하면 TLS)")


def broker_options(args: argparse.Namespace) -> dict:
    """host, port, username, password and an ``ssl`` context from the options of ``add_broker_args``."""
    tls = ssl.create_default_context(cafile=args.cafile) if args.tls or args.cafile else None
    host, _, port = args.broker.rpartition(":") if args.broker.count(":") == 1 else (args.broker, "", "")
    return {"host": host, "port": int(port or (8883 if tls else 1883)), "username": args.mqtt_user,
            "password": os.environ.get("FCT_MQTT_PASSWORD") if args.mqtt_user else None, "tls": tls}


def qos_retain(topic: str) -> tuple[int, bool]:
    """Delivery rules per topic (VDA 5050 2.x section 6: connection QoS 1 retained, the rest QoS 0)."""
    if topic.endswith("/connection"):
        return 1, True
    if topic.startswith("facility/") and topic.endswith("/state"):
        return 0, True                    # the latest door / fire panel state is there for later subscribers
    if topic == "fleet/v1/fms/state":
        return 1, True                    # the manager's own ONLINE / OFFLINE, like a vehicle's connection
    return 0, False


class MqttBus:
    def __init__(self, client: MqttClient, clock: WallClock, log_depth: int = 30) -> None:
        self.client = client
        self.clock = clock
        self._subs: list[tuple[str, Handler]] = []
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.now = 0.0
        self.stats = {"published": 0, "delivered": 0, "dropped": 0, "bytes": 0, "invalid_json": 0}
        self.links: dict[str, LinkQuality] = {}     # the simulation's link model has no meaning here
        self.log: dict[str, deque] = {}
        self._log_depth = log_depth
        client.on_message = self._on_message

    # ------------------------------------------------------------ Bus interface
    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))
        if not any(p == pattern for p, _ in self._subs[:-1]):
            # without a network thread or connection yet the client keeps it and subscribes on connect
            self.client.subscribe(pattern, qos=1 if pattern.endswith("/connection") else 0, timeout=None)

    def publish(self, topic: str, payload: dict, now: float, retain: bool | None = None) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        qos, keep = qos_retain(topic)
        ok = self.client.publish(topic, data, qos=qos, retain=keep if retain is None else retain)
        self.stats["published"] += 1
        self.stats["bytes"] += len(data)
        if not ok:
            self.stats["dropped"] += 1
        self._record(topic, payload, now, dropped=not ok)
        return ok

    def deliver_due(self, now: float) -> int:
        """Hand every message received so far to its handlers (on the caller's thread)."""
        self.now = now
        delivered = 0
        while True:
            try:
                topic, raw, retained = self._inbox.get_nowait()
            except queue.Empty:
                break
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, dict):
                self.stats["invalid_json"] += 1
                continue
            if retained and topic.startswith("facility/v1/door/"):
                # the broker's copy of an old door state, not the controller speaking now: the door supervisor
                # must not treat it as a live answer (a dead controller's last OPEN would let traffic in)
                payload["_retained"] = True
            self._record(topic, payload, now, dropped=False, inbound=True)
            for pattern, handler in self._subs:
                if topic_matches(pattern, topic):
                    handler(topic, payload)
            delivered += 1
        self.stats["delivered"] += delivered
        return delivered

    def link(self, serial: str) -> LinkQuality:
        return self.links.setdefault(serial, LinkQuality())

    def recent(self, serial: str, limit: int = 12) -> list[dict]:
        return list(self.log.get(serial, ()))[-limit:]

    # ------------------------------------------------------------ internals
    def _on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        self._inbox.put((topic, payload, retain))

    def _record(self, topic: str, payload: dict, now: float, dropped: bool, inbound: bool = False) -> None:
        parsed = split_topic(topic)
        if parsed is None or parsed[2] == "visualization":
            return
        serial = parsed[1]
        if serial not in self.log:
            self.log[serial] = deque(maxlen=self._log_depth)
        self.log[serial].append({"t": round(now, 2), "topic": topic, "dropped": dropped, "in": inbound,
                                 "payload": payload})
