"""Check fleet.mqtt against a real MQTT broker (mosquitto, EMQX, HiveMQ, ...).

The unit tests run the client against ``fleet.minibroker``; this runs the same
core scenarios against a production broker, so the client is not only
compatible with its own test double::

    docker run -d --rm --name fct-mosquitto-test -p 127.0.0.1:18830:1883 \\
        eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
    python3 tools/mqtt_interop_check.py --host 127.0.0.1 --port 18830
    docker stop fct-mosquitto-test

Topics live under a random prefix and retained messages are cleared at the
end. Exit status 0 when every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet.mqtt import MqttClient, Will  # noqa: E402
from fleet.vda5050 import HeaderIds, build_connection, header, topic as vda_topic  # noqa: E402


class Inbox:
    def __init__(self) -> None:
        self.items: list[tuple[str, bytes, bool]] = []
        self._cond = threading.Condition()

    def __call__(self, topic: str, payload: bytes, retain: bool) -> None:
        with self._cond:
            self.items.append((topic, payload, retain))
            self._cond.notify_all()

    def wait_for(self, predicate, timeout: float = 5.0) -> tuple[str, bytes, bool]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for item in self.items:
                    if predicate(item):
                        return item
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no matching message within {timeout} s (got {[m[0] for m in self.items]})")
                self._cond.wait(left)

    def topics(self) -> list[str]:
        return [m[0] for m in list(self.items)]


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(what)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


class Checker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = uuid.uuid4().hex[:8]
        self.prefix = f"fct-interop/{self.run_id}"
        self.clients: list[MqttClient] = []
        self.retained_topics: set[str] = set()
        self.tls = None
        if args.tls or args.cafile:
            self.tls = ssl.create_default_context(cafile=args.cafile)
            if args.insecure:
                self.tls.check_hostname = False
                self.tls.verify_mode = ssl.CERT_NONE

    def client(self, name: str, start: bool = True, **kw) -> tuple[MqttClient, Inbox]:
        kw.setdefault("reconnect_delay", (0.5, 2.0))
        c = MqttClient(self.args.host, self.args.port, f"fct-{self.run_id}-{name}", username=self.args.username,
                       password=self.args.password, tls=self.tls, **kw)
        inbox = Inbox()
        c.on_message = inbox
        self.clients.append(c)
        if start:
            c.connect(timeout=5.0)
            c.start()
        return c, inbox

    def t(self, *parts: str) -> str:
        return "/".join((self.prefix, *parts))

    def barrier(self, c: MqttClient, inbox: Inbox) -> None:
        topic = self.t("barrier", c.client_id)
        c.subscribe(topic, 1)
        marker = os.urandom(8)
        expect(c.publish(topic, marker, qos=1, timeout=5.0), "barrier PUBACK")
        inbox.wait_for(lambda m: m[0] == topic and m[1] == marker)

    # -- checks: each returns a short detail string or raises

    def check_connect(self) -> str:
        c, _ = self.client("connect")
        expect(c.connected, "connected after CONNACK")
        return f"CONNACK accepted, session_present={c.session_present}"

    def check_round_trip(self) -> str:
        sub, inbox = self.client("rt-sub")
        pub, _ = self.client("rt-pub")
        expect(sub.subscribe(self.t("rt", "q0"), 0) == 0, "SUBACK granted QoS 0")
        expect(sub.subscribe(self.t("rt", "q1"), 1) == 1, "SUBACK granted QoS 1")
        expect(pub.publish(self.t("rt", "q0"), b"zero"), "QoS 0 written")
        expect(pub.publish(self.t("rt", "q1"), "하나".encode(), qos=1, timeout=5.0), "QoS 1 PUBACK")
        expect(pub.inflight == 0, "nothing left in flight")
        expect(inbox.wait_for(lambda m: m[0] == self.t("rt", "q0"))[1] == b"zero", "QoS 0 payload")
        expect(inbox.wait_for(lambda m: m[0] == self.t("rt", "q1"))[1] == "하나".encode(), "QoS 1 payload")
        return "QoS 0 and QoS 1 (PUBACK) delivered"

    def check_wildcards(self) -> str:
        plus, plus_in = self.client("wc-plus")
        hash_, hash_in = self.client("wc-hash")
        pub, _ = self.client("wc-pub")
        plus.subscribe(self.t("uagv", "+", "+", "state"))
        hash_.subscribe(self.t("uagv", "sim", "#"))
        sent = [("sim", "AMR-01", "state"), ("sim", "AMR-02", "order"), ("other", "AMR-09", "state"),
                ("sim", "AMR-01", "state", "extra"), ("sim",)]
        for parts in sent:
            expect(pub.publish(self.t("uagv", *parts), b"{}", qos=1, timeout=5.0), "PUBACK")
        self.barrier(plus, plus_in)
        self.barrier(hash_, hash_in)
        got_plus = [t for t in plus_in.topics() if "/uagv/" in t]
        got_hash = [t for t in hash_in.topics() if "/uagv/" in t]
        expect(got_plus == [self.t("uagv", "sim", "AMR-01", "state"), self.t("uagv", "other", "AMR-09", "state")],
               f"'+' matched {got_plus}")
        expect(got_hash == [self.t("uagv", "sim", "AMR-01", "state"), self.t("uagv", "sim", "AMR-02", "order"),
                            self.t("uagv", "sim", "AMR-01", "state", "extra"), self.t("uagv", "sim")],
               f"'#' matched {got_hash}")
        return "'+' one level, '#' tail and parent level"

    def check_retained(self) -> str:
        topic = self.t("door", "D1")
        pub, _ = self.client("ret-pub")
        self.retained_topics.add(topic)
        expect(pub.publish(topic, b"OPEN", qos=1, retain=True, timeout=5.0), "PUBACK")
        late, late_in = self.client("ret-late")
        late.subscribe(self.t("door", "+"), 1)
        got = late_in.wait_for(lambda m: m[0] == topic)
        expect(got[1:] == (b"OPEN", True), f"late subscriber got {got}")
        expect(pub.publish(topic, b"", qos=1, retain=True, timeout=5.0), "PUBACK for clearing")
        again, again_in = self.client("ret-again")
        again.subscribe(self.t("door", "#"), 1)
        self.barrier(again, again_in)
        expect(topic not in again_in.topics(), "cleared retained message still delivered")
        return "late subscriber got it with retain=1; empty payload cleared it"

    def _vda(self, serial: str):
        ids = HeaderIds()
        conn_topic = self.t(vda_topic("sim", serial, "connection"))

        def message(state: str) -> bytes:
            return json.dumps(build_connection(header(ids, "connection", "sim", serial, 0.0), state)).encode()
        return conn_topic, message

    def check_will_on_socket_loss(self) -> str:
        conn_topic, message = self._vda(f"AMR-{self.run_id}-A")
        self.retained_topics.add(conn_topic)
        fms, fms_in = self.client("will-fms")
        fms.subscribe(self.t("uagv", "v2", "+", "+", "connection"), 1)
        vehicle, _ = self.client("will-vehicle", will=Will(conn_topic, message("CONNECTIONBROKEN"), 1, True))
        expect(vehicle.publish(conn_topic, message("ONLINE"), qos=1, retain=True, timeout=5.0), "PUBACK")

        def state(m):
            return m[0] == conn_topic and json.loads(m[1])["connectionState"]
        fms_in.wait_for(lambda m: state(m) == "ONLINE")
        vehicle.stop()                           # socket closed without DISCONNECT
        fms_in.wait_for(lambda m: state(m) == "CONNECTIONBROKEN")
        late, late_in = self.client("will-late")
        late.subscribe(conn_topic, 1)
        got = late_in.wait_for(lambda m: m[0] == conn_topic)
        expect(json.loads(got[1])["connectionState"] == "CONNECTIONBROKEN" and got[2], f"retained will {got}")
        return "ONLINE -> CONNECTIONBROKEN (Will, retained)"

    def check_no_will_on_disconnect(self) -> str:
        conn_topic, message = self._vda(f"AMR-{self.run_id}-B")
        self.retained_topics.add(conn_topic)
        fms, fms_in = self.client("nowill-fms")
        fms.subscribe(self.t("uagv", "v2", "+", "+", "connection"), 1)
        vehicle, _ = self.client("nowill-vehicle", will=Will(conn_topic, message("CONNECTIONBROKEN"), 1, True))
        vehicle.publish(conn_topic, message("ONLINE"), qos=1, retain=True, timeout=5.0)
        vehicle.publish(conn_topic, message("OFFLINE"), qos=1, retain=True, timeout=5.0)
        vehicle.disconnect()
        time.sleep(1.0)                          # a Will would be published right after the close
        self.barrier(fms, fms_in)
        states = [json.loads(p)["connectionState"] for t, p, _ in fms_in.items if t == conn_topic]
        expect(states == ["ONLINE", "OFFLINE"], f"connection states {states}")
        return "ONLINE -> OFFLINE, no CONNECTIONBROKEN"

    def check_keepalive(self) -> str:
        c, inbox = self.client("keepalive", keepalive=2)
        drops: list[str] = []
        c.on_disconnect = drops.append
        time.sleep(4.0)                          # past 1.5 x keepalive without application traffic
        expect(not drops and c.connected, f"dropped: {drops}")
        self.barrier(c, inbox)
        return "idle 4 s at keepalive 2 s, still connected"

    def check_large_payload(self) -> str:
        sub, inbox = self.client("big-sub")
        pub, _ = self.client("big-pub")
        sub.subscribe(self.t("blob"), 1)
        payload = os.urandom(100 * 1024)
        expect(pub.publish(self.t("blob"), payload, qos=1, timeout=5.0), "PUBACK")
        expect(inbox.wait_for(lambda m: m[0] == self.t("blob"))[1] == payload, "payload intact")
        return "100 KiB QoS 1 round trip intact"

    def check_reconnect_resubscribe(self) -> str:
        # a second connection with the same client id makes the broker drop the first one
        name = "reconnect"
        c, inbox = self.client(name)
        ups: list[float] = []
        downs: list[str] = []
        c.on_connect = lambda: ups.append(time.monotonic())
        c.on_disconnect = downs.append
        c.subscribe(self.t("cmd", "#"), 1)
        intruder = MqttClient(self.args.host, self.args.port, c.client_id, username=self.args.username,
                              password=self.args.password, tls=self.tls)
        intruder.connect(timeout=5.0)
        intruder.disconnect()
        expect(wait_until(lambda: downs, 5.0), "first connection was not dropped on takeover")
        expect(wait_until(lambda: ups, 10.0), "no reconnection")
        pub, _ = self.client("reconnect-pub")
        expect(pub.publish(self.t("cmd", "go"), b"go", qos=1, timeout=5.0), "PUBACK")
        inbox.wait_for(lambda m: m[0] == self.t("cmd", "go"))
        return f"dropped ({downs[0]}), reconnected, subscription restored"

    def info_sys_version(self) -> str:
        c, inbox = self.client("sys")
        c.subscribe("$SYS/broker/version")
        try:
            return inbox.wait_for(lambda m: m[0] == "$SYS/broker/version", 3.0)[1].decode(errors="replace")
        except AssertionError:
            return "not published by this broker"

    def cleanup(self) -> None:
        try:
            if self.retained_topics:
                c, _ = self.client("cleanup")
                for topic in sorted(self.retained_topics):
                    c.publish(topic, b"", qos=1, retain=True, timeout=5.0)
        except Exception as exc:
            print(f"[WARN] could not clear retained test messages: {type(exc).__name__}: {exc}")
        finally:
            for c in self.clients:
                c.disconnect()
                c.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run fleet.mqtt against a real MQTT 3.1.1 broker.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--tls", action="store_true", help="connect with TLS (system trust store unless --cafile)")
    ap.add_argument("--cafile", help="CA certificate for the broker (implies --tls)")
    ap.add_argument("--insecure", action="store_true", help="TLS without certificate or host name checks")
    args = ap.parse_args()

    checker = Checker(args)
    checks = [
        ("connect", checker.check_connect),
        ("publish/subscribe QoS 0 and 1", checker.check_round_trip),
        ("wildcards + and #", checker.check_wildcards),
        ("retained message, late subscriber, clear", checker.check_retained),
        ("Will on socket loss (VDA 5050 connection)", checker.check_will_on_socket_loss),
        ("no Will after DISCONNECT", checker.check_no_will_on_disconnect),
        ("keepalive PINGREQ", checker.check_keepalive),
        ("100 KiB payload", checker.check_large_payload),
        ("reconnect and resubscribe", checker.check_reconnect_resubscribe),
    ]
    print(f"broker {args.host}:{args.port}{' (TLS)' if checker.tls else ''}, topic prefix {checker.prefix}")
    failed = 0
    try:
        try:
            print(f"[INFO] $SYS/broker/version: {checker.info_sys_version()}")
        except Exception as exc:  # the connect check below reports the failure properly
            print(f"[INFO] $SYS/broker/version: unavailable ({type(exc).__name__}: {exc})")
        for name, fn in checks:
            start = time.monotonic()
            try:
                detail = fn()
                print(f"[PASS] {name} ({time.monotonic() - start:.2f} s): {detail}")
            except Exception as exc:  # report every scenario, keep going
                failed += 1
                print(f"[FAIL] {name} ({time.monotonic() - start:.2f} s): {type(exc).__name__}: {exc}")
    finally:
        checker.cleanup()
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
