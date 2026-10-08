"""MQTT 3.1.1 client and mini broker: codec, routing, retained, Will, QoS 1, reconnect, keepalive."""
from __future__ import annotations

import doctest
import json
import os
import socket
import threading
import time
import unittest

from fleet import mqtt as mqtt_mod
from fleet.minibroker import MiniBroker
from fleet.mqtt import (CONNACK, CONNECT, PUBACK, PUBLISH, SUBACK, SUBACK_FAILURE, ConnectRefused, MqttClient,
                        PacketTooLarge, ProtocolError, Will, check_topic_filter, check_topic_name,
                        decode_remaining_length, encode_connect, encode_publish, encode_remaining_length,
                        encode_str, encode_subscribe, packet, parse_publish, read_str, take_packet, topic_matches)
from fleet.vda5050 import HeaderIds, build_connection, header, topic as vda_topic

HOST = "127.0.0.1"
FAST = (0.1, 1.0)            # reconnect backoff for tests (the default is 1 -> 30 s)
CONNACK_OK = packet(CONNACK, 0, b"\x00\x00")


def load_tests(loader, tests, ignore):
    tests.addTests(doctest.DocTestSuite(mqtt_mod))
    return tests


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class Inbox:
    """on_message sink that a test can wait on."""

    def __init__(self) -> None:
        self.items: list[tuple[str, bytes, bool]] = []
        self._cond = threading.Condition()

    def __call__(self, topic: str, payload: bytes, retain: bool) -> None:
        with self._cond:
            self.items.append((topic, payload, retain))
            self._cond.notify_all()

    def wait_for(self, predicate, timeout: float = 3.0) -> tuple[str, bytes, bool]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for item in self.items:
                    if predicate(item):
                        return item
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no matching message within {timeout} s; got {self.topics()}")
                self._cond.wait(left)

    def wait_topic(self, topic: str, timeout: float = 3.0) -> tuple[str, bytes, bool]:
        return self.wait_for(lambda m: m[0] == topic, timeout)

    def topics(self) -> list[str]:
        return [m[0] for m in list(self.items)]

    def payloads(self, topic: str) -> list[bytes]:
        return [p for t, p, _ in list(self.items) if t == topic]


class Peer:
    """A bare socket speaking MQTT by hand, to see exactly what the other side sends."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.sock.settimeout(3.0)
        self.buf = bytearray()

    @classmethod
    def connect(cls, port: int, client_id: str, keepalive: int = 0, will: Will | None = None,
                clean: bool = True) -> Peer:
        peer = cls(socket.create_connection((HOST, port), timeout=3.0))
        peer.send(encode_connect(client_id, keepalive, clean, will))
        return peer

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self) -> tuple[int, int, bytes]:
        while (pkt := take_packet(self.buf)) is None:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buf += data
        return pkt

    def closed_by_peer(self, timeout: float = 3.0) -> bool:
        self.sock.settimeout(timeout)
        try:
            while self.sock.recv(65536):
                pass
            return True
        except ConnectionResetError:
            return True
        except TimeoutError:
            return False

    def close(self) -> None:
        self.sock.close()


class FakeBroker:
    """A listening socket the test drives by hand, playing the broker."""

    def __init__(self) -> None:
        self.listener = socket.create_server((HOST, 0))
        self.listener.settimeout(3.0)
        self.port = self.listener.getsockname()[1]

    def accept(self) -> Peer:
        sock, _ = self.listener.accept()
        return Peer(sock)

    def close(self) -> None:
        self.listener.close()


def leftover_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith(("mqtt-", "minibroker"))]


class CodecTests(unittest.TestCase):
    def test_remaining_length_round_trip_and_limits(self):
        for n in (0, 1, 127, 128, 16_383, 16_384, 2_097_151, 2_097_152, 268_435_455):
            encoded = encode_remaining_length(n)
            self.assertEqual(decode_remaining_length(b"\x30" + encoded), (n, 1 + len(encoded)))
        self.assertEqual([len(encode_remaining_length(n)) for n in (127, 128, 16_384, 2_097_152)], [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            encode_remaining_length(268_435_456)
        with self.assertRaises(ProtocolError):
            decode_remaining_length(b"\x30\xff\xff\xff\xff\x01")

    def test_take_packet_waits_for_the_whole_packet(self):
        pkt = encode_publish("a/b", b"xyz")
        buf = bytearray(pkt[:3])
        self.assertIsNone(take_packet(buf))
        buf += pkt[3:] + b"\xc0"                  # and the first byte of the next packet
        self.assertEqual(take_packet(buf), (PUBLISH, 0, pkt[2:]))
        self.assertEqual(buf, bytearray(b"\xc0"))

    def test_oversized_packet_is_refused_from_its_header(self):
        header_only = bytearray(b"\x30" + encode_remaining_length(2 * 1024 * 1024))
        with self.assertRaises(PacketTooLarge):
            take_packet(header_only, max_size=1024 * 1024)

    def test_utf8_strings(self):
        self.assertEqual(read_str(encode_str("지게차/AMR-01"), 0), ("지게차/AMR-01", len(encode_str("지게차/AMR-01"))))
        with self.assertRaises(ValueError):
            encode_str("a\x00b")
        with self.assertRaises(ProtocolError):
            read_str(b"\x00\x02\xff\xfe", 0)

    def test_topic_matching(self):
        match = [("#", "a"), ("#", "a/b/c"), ("a/#", "a"), ("a/#", "a/b/c"), ("a/+/c", "a/b/c"), ("+", "a"),
                 ("+/+", "/x"), ("+/x", "/x"), ("a/+", "a/"), ("$SYS/#", "$SYS/broker"),
                 ("$SYS/+/load", "$SYS/broker/load"), ("uagv/v2/+/+/state", "uagv/v2/sim/AMR-01/state")]
        no_match = [("a/+", "a/b/c"), ("a/+", "a"), ("+", "/x"), ("a/b", "a/b/c"), ("a/b/c", "a/b"),
                    ("#", "$SYS/broker"), ("+/broker", "$SYS/broker"), ("a/b/#", "a/c"),
                    ("uagv/v2/+/AMR-02/state", "uagv/v2/sim/AMR-01/state")]
        for f, t in match:
            self.assertTrue(topic_matches(f, t), (f, t))
        for f, t in no_match:
            self.assertFalse(topic_matches(f, t), (f, t))

    def test_topic_validation(self):
        for good in ("#", "+", "a/+/b", "a/#", "+/+/#", "/"):
            check_topic_filter(good)
        for bad in ("", "a/#/b", "a#", "a/b#", "a/b+", "+a"):
            with self.assertRaises(ValueError, msg=bad):
                check_topic_filter(bad)
        for bad in ("", "a/+", "a/#", "a\x00b"):
            with self.assertRaises(ValueError, msg=bad):
                check_topic_name(bad)

    def test_client_rejects_bad_arguments(self):
        c = MqttClient(HOST, 1, "offline", max_packet=1024)
        with self.assertRaises(PacketTooLarge):
            c.publish("a", b"x" * 2000)
        with self.assertRaises(ValueError):
            c.publish("a/+", b"")
        with self.assertRaises(ValueError):
            c.publish("a", b"", qos=2)
        with self.assertRaises(TypeError):
            c.publish("a", "text")
        with self.assertRaises(ValueError):
            c.subscribe("a/#/b")
        self.assertFalse(c.publish("a", b"x"))              # QoS 0 while offline is dropped
        with self.assertRaises(ValueError):
            MqttClient(HOST, 1, "x", password="secret")      # 3.1.1: password only with a user name
        with self.assertRaises(ValueError):
            MqttClient(HOST, 1, "", clean_session=False)


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._assert_no_threads)          # registered first, so it runs last
        self.broker = MiniBroker().start()
        self.addCleanup(self.broker.stop)

    def _assert_no_threads(self):
        if not wait_until(lambda: not leftover_threads(), 3.0):
            self.fail(f"threads left running: {leftover_threads()}")

    def client(self, client_id: str, **kw) -> tuple[MqttClient, Inbox]:
        kw.setdefault("reconnect_delay", FAST)
        c = MqttClient(HOST, self.broker.port, client_id, **kw)
        inbox = Inbox()
        c.on_message = inbox
        c.connect(timeout=2.0)
        c.start()
        self.addCleanup(c.stop)
        return c, inbox

    def peer(self, client_id: str, **kw) -> Peer:
        p = Peer.connect(self.broker.port, client_id, **kw)
        self.addCleanup(p.close)
        return p

    def barrier(self, c: MqttClient, inbox: Inbox) -> None:
        """Round trip through the broker: whatever it routed to ``c`` before this has arrived."""
        topic = f"test/barrier/{c.client_id}"
        c.subscribe(topic, 1)
        marker = os.urandom(8)
        self.assertTrue(c.publish(topic, marker, qos=1, timeout=2.0))
        inbox.wait_for(lambda m: m[0] == topic and m[1] == marker)


class RoutingTests(BrokerTestCase):
    def test_publish_subscribe_round_trip(self):
        sub, inbox = self.client("sub")
        pub, _ = self.client("pub")
        self.assertEqual(sub.subscribe("fleet/test/q0", 0), 0)
        self.assertEqual(sub.subscribe("fleet/test/q1", 1), 1)
        self.assertTrue(pub.publish("fleet/test/q0", b"zero"))
        self.assertTrue(pub.publish("fleet/test/q1", "하나".encode(), qos=1, timeout=2.0))
        self.assertEqual(inbox.wait_topic("fleet/test/q0"), ("fleet/test/q0", b"zero", False))
        self.assertEqual(inbox.wait_topic("fleet/test/q1"), ("fleet/test/q1", "하나".encode(), False))

    def test_wildcards(self):
        state_sub, state_in = self.client("state-sub")
        site_sub, site_in = self.client("site-sub")
        pub, _ = self.client("pub")
        state_sub.subscribe("uagv/v2/+/+/state")
        site_sub.subscribe("uagv/v2/sim/#")
        for t in ("uagv/v2/sim/AMR-01/state", "uagv/v2/sim/AMR-02/order", "uagv/v2/other/AMR-09/state",
                  "uagv/v2/sim/AMR-01/state/extra", "uagv/v2/sim"):
            self.assertTrue(pub.publish(t, b"{}", qos=1, timeout=2.0))
        self.barrier(state_sub, state_in)
        self.barrier(site_sub, site_in)
        self.assertEqual([t for t in state_in.topics() if t.startswith("uagv")],
                         ["uagv/v2/sim/AMR-01/state", "uagv/v2/other/AMR-09/state"])
        self.assertEqual([t for t in site_in.topics() if t.startswith("uagv")],
                         ["uagv/v2/sim/AMR-01/state", "uagv/v2/sim/AMR-02/order",
                          "uagv/v2/sim/AMR-01/state/extra", "uagv/v2/sim"])

    def test_unsubscribe_stops_delivery(self):
        sub, inbox = self.client("sub")
        pub, _ = self.client("pub")
        sub.subscribe("door/D1")
        pub.publish("door/D1", b"1", qos=1, timeout=2.0)
        inbox.wait_topic("door/D1")
        self.assertTrue(sub.unsubscribe("door/D1"))
        pub.publish("door/D1", b"2", qos=1, timeout=2.0)
        self.barrier(sub, inbox)
        self.assertEqual(inbox.payloads("door/D1"), [b"1"])

    def test_retained_message_for_late_subscriber_and_cleared_by_empty_payload(self):
        pub, _ = self.client("pub")
        live, live_in = self.client("live")
        live.subscribe("site/door/D1")
        self.assertTrue(pub.publish("site/door/D1", b"OPEN", qos=1, retain=True, timeout=2.0))
        # a live subscriber gets an ordinary copy; only the stored one is flagged retained
        self.assertEqual(live_in.wait_topic("site/door/D1"), ("site/door/D1", b"OPEN", False))
        late, late_in = self.client("late")
        late.subscribe("site/door/+")
        self.assertEqual(late_in.wait_topic("site/door/D1"), ("site/door/D1", b"OPEN", True))

        self.assertTrue(pub.publish("site/door/D1", b"", qos=1, retain=True, timeout=2.0))
        live_in.wait_for(lambda m: m == ("site/door/D1", b"", False))      # forwarded as usual
        self.assertEqual(self.broker.retained(), {})
        again, again_in = self.client("again")
        again.subscribe("site/#")
        self.barrier(again, again_in)
        self.assertNotIn("site/door/D1", again_in.topics())

    def test_delivery_qos_is_the_lower_of_publish_and_subscription(self):
        raw = self.peer("raw")
        self.assertEqual(raw.read()[0], CONNACK)
        raw.send(encode_subscribe(7, [("q/zero", 0), ("q/one", 1), ("q/#/bad", 1), ("q/two", 2)]))
        ptype, _, body = raw.read()
        self.assertEqual((ptype, body), (SUBACK, b"\x00\x07" + bytes([0, 1, SUBACK_FAILURE, 1])))
        pub, _ = self.client("pub")
        self.assertTrue(pub.publish("q/one", b"a", qos=1, timeout=2.0))
        self.assertEqual(pub.inflight, 0)
        self.assertTrue(pub.publish("q/zero", b"b", qos=1, timeout=2.0))
        self.assertTrue(pub.publish("q/one", b"c", qos=0))
        topic, payload, qos, retain, dup, mid = parse_publish(*raw.read()[1:])
        self.assertEqual((topic, payload, qos, retain, dup), ("q/one", b"a", 1, False, False))
        raw.send(packet(PUBACK, 0, mid.to_bytes(2, "big")))
        self.assertEqual(parse_publish(*raw.read()[1:])[:3], ("q/zero", b"b", 0))
        self.assertEqual(parse_publish(*raw.read()[1:])[:3], ("q/one", b"c", 0))

    def test_100kb_payload(self):
        sub, inbox = self.client("sub")
        pub, _ = self.client("pub")
        sub.subscribe("blob", 1)
        payload = os.urandom(100 * 1024)
        self.assertTrue(pub.publish("blob", payload, qos=1, timeout=3.0))
        self.assertEqual(inbox.wait_topic("blob")[1], payload)

    def test_packet_size_limit_on_both_sides(self):
        small, _ = self.client("small", max_packet=64 * 1024)
        reasons: list[str] = []
        small.on_disconnect = reasons.append
        small.subscribe("blob")
        pub, _ = self.client("pub")
        self.assertTrue(pub.publish("blob", os.urandom(100 * 1024), qos=1, timeout=3.0))
        self.assertTrue(wait_until(lambda: reasons))
        self.assertIn("exceeds", reasons[0])                     # the client refused the incoming packet
        raw = self.peer("raw")
        raw.read()
        raw.send(b"\x30" + encode_remaining_length(2 * 1024 * 1024))
        self.assertTrue(raw.closed_by_peer())                    # and the broker refuses one over 1 MB


class ConnectionTests(BrokerTestCase):
    def vda_connection(self, serial: str):
        ids = HeaderIds()

        def message(state: str) -> bytes:
            return json.dumps(build_connection(header(ids, "connection", "sim", serial, 0.0), state)).encode()
        return vda_topic("sim", serial, "connection"), message

    def test_will_on_socket_loss_vda5050_connection_topic(self):
        conn_topic, message = self.vda_connection("AMR-01")
        fms, fms_in = self.client("fms")
        fms.subscribe("uagv/v2/+/+/connection", 1)
        will = Will(conn_topic, message("CONNECTIONBROKEN"), qos=1, retain=True)
        vehicle, _ = self.client("AMR-01", will=will)
        self.assertTrue(vehicle.publish(conn_topic, message("ONLINE"), qos=1, retain=True, timeout=2.0))

        def state(m):
            return m[0] == conn_topic and json.loads(m[1])["connectionState"]
        fms_in.wait_for(lambda m: state(m) == "ONLINE")
        vehicle.stop()                                            # process dies: no DISCONNECT
        fms_in.wait_for(lambda m: state(m) == "CONNECTIONBROKEN")
        late, late_in = self.client("late")
        late.subscribe(conn_topic)
        topic, payload, retain = late_in.wait_topic(conn_topic)
        self.assertEqual((json.loads(payload)["connectionState"], retain), ("CONNECTIONBROKEN", True))

    def test_clean_disconnect_publishes_no_will(self):
        conn_topic, message = self.vda_connection("AMR-02")
        fms, fms_in = self.client("fms")
        fms.subscribe("uagv/v2/+/+/connection", 1)
        vehicle, _ = self.client("AMR-02", will=Will(conn_topic, message("CONNECTIONBROKEN"), 1, True))
        reasons: list[str] = []
        vehicle.on_disconnect = reasons.append
        vehicle.publish(conn_topic, message("ONLINE"), qos=1, retain=True, timeout=2.0)
        vehicle.publish(conn_topic, message("OFFLINE"), qos=1, retain=True, timeout=2.0)
        vehicle.disconnect()
        self.assertEqual(reasons, ["disconnect requested"])
        self.assertTrue(wait_until(lambda: "AMR-02" not in self.broker.client_ids()))
        self.barrier(fms, fms_in)
        self.assertEqual([json.loads(p)["connectionState"] for p in fms_in.payloads(conn_topic)],
                         ["ONLINE", "OFFLINE"])
        self.assertEqual(json.loads(self.broker.retained()[conn_topic])["connectionState"], "OFFLINE")

    def test_broker_drops_a_silent_client_after_one_and_a_half_keepalives(self):
        mon, mon_in = self.client("mon")
        mon.subscribe("amr/+/connection")
        raw = self.peer("silent", keepalive=1, will=Will("amr/silent/connection", b"CONNECTIONBROKEN"))
        self.assertEqual(raw.read(), (CONNACK, 0, b"\x00\x00"))
        start = time.monotonic()
        mon_in.wait_topic("amr/silent/connection", timeout=4.0)
        self.assertGreaterEqual(time.monotonic() - start, 1.2)
        self.assertTrue(raw.closed_by_peer(1.0))

    def test_keepalive_pings_hold_an_idle_connection(self):
        c, inbox = self.client("idle", keepalive=1)
        drops: list[str] = []
        c.on_disconnect = drops.append
        time.sleep(2.6)                                           # well past 1.5 x keepalive, no traffic
        self.assertEqual(drops, [])
        self.assertTrue(c.connected)
        self.assertGreaterEqual(self.broker.stats["PINGREQ"], 2)
        self.barrier(c, inbox)

    def test_same_client_id_takes_over_without_will(self):
        mon, mon_in = self.client("mon")
        mon.subscribe("amr/+/connection")
        old = self.peer("AMR-03", will=Will("amr/AMR-03/connection", b"CONNECTIONBROKEN"))
        self.assertEqual(old.read()[0], CONNACK)
        new, new_in = self.client("AMR-03")
        self.assertTrue(old.closed_by_peer())
        self.barrier(new, new_in)
        self.barrier(mon, mon_in)
        self.assertNotIn("amr/AMR-03/connection", mon_in.topics())
        self.assertIn("AMR-03", self.broker.client_ids())

    def test_broker_rejects_bad_connects(self):
        anonymous = self.peer("", clean=False)                   # persistent session needs a client id
        self.assertEqual(anonymous.read(), (CONNACK, 0, b"\x00\x02"))
        self.assertTrue(anonymous.closed_by_peer())
        future = Peer(socket.create_connection((HOST, self.broker.port), timeout=3.0))
        self.addCleanup(future.close)
        future.send(packet(CONNECT, 0, encode_str("MQTT") + bytes([5, 0x02, 0, 0]) + encode_str("v5")))
        self.assertEqual(future.read(), (CONNACK, 0, b"\x00\x01"))
        self.assertTrue(future.closed_by_peer())

    def test_reconnect_and_resubscribe_after_broker_restart(self):
        port = self.broker.port
        c, inbox = self.client("AMR-07")
        connects: list[float] = []
        reasons: list[str] = []
        c.on_connect = lambda: connects.append(time.monotonic())
        c.on_disconnect = reasons.append
        c.subscribe("fleet/cmd/#", 1)
        self.broker.stop()
        self.assertTrue(wait_until(lambda: reasons))
        self.assertFalse(c.connected)
        self.assertTrue(c.publish("fleet/cmd/queued", b"q1", qos=1))    # kept for after the reconnect
        self.assertFalse(c.publish("fleet/cmd/lost", b"q0"))            # QoS 0 offline is dropped
        self.broker = MiniBroker(port=port).start()
        self.addCleanup(self.broker.stop)
        self.assertTrue(wait_until(lambda: connects, 5.0))
        # resubscribed before the resend, so the queued message loops back to its sender
        self.assertEqual(inbox.wait_topic("fleet/cmd/queued")[1], b"q1")
        self.assertTrue(wait_until(lambda: c.inflight == 0))
        fms, _ = self.client("fms")
        self.assertTrue(fms.publish("fleet/cmd/go", b"go", qos=1, timeout=2.0))
        self.assertEqual(inbox.wait_topic("fleet/cmd/go")[1], b"go")
        self.assertNotIn("fleet/cmd/lost", inbox.topics())


class ScriptedBrokerTests(unittest.TestCase):
    """Client behaviour that needs a broker misbehaving on purpose."""

    def setUp(self):
        self.addCleanup(lambda: self.assertTrue(wait_until(lambda: not leftover_threads()), leftover_threads()))
        self.server = FakeBroker()
        self.addCleanup(self.server.close)

    def accept(self) -> Peer:
        peer = self.server.accept()
        self.addCleanup(peer.close)
        return peer

    def test_unacknowledged_qos1_is_resent_with_dup_after_reconnect(self):
        c = MqttClient(HOST, self.server.port, "dup-test", reconnect_delay=FAST)
        self.addCleanup(c.stop)
        up = threading.Event()
        c.on_connect = up.set
        c.start()                                                 # the thread makes the first connection
        first = self.accept()
        self.assertEqual(first.read()[0], CONNECT)
        first.send(CONNACK_OK)
        self.assertTrue(up.wait(3.0))
        up.clear()
        self.assertTrue(c.publish("amr/log", b"hello", qos=1))
        ptype, flags, body = first.read()
        topic, payload, qos, _, dup, mid = parse_publish(flags, body)
        self.assertEqual((ptype, qos, dup), (PUBLISH, 1, False))
        first.close()                                             # lost before PUBACK

        second = self.accept()
        self.assertEqual(second.read()[0], CONNECT)
        second.send(CONNACK_OK)
        ptype, flags, body = second.read()
        self.assertEqual(parse_publish(flags, body), ("amr/log", b"hello", 1, False, True, mid))
        self.assertEqual(c.inflight, 1)
        second.send(packet(PUBACK, 0, mid.to_bytes(2, "big")))
        self.assertTrue(wait_until(lambda: c.inflight == 0))
        self.assertTrue(up.is_set())

    def test_connack_refusal_reports_the_reason(self):
        def refuse():
            peer = self.server.accept()
            peer.read()
            peer.send(packet(CONNACK, 0, b"\x00\x05"))
            peers.append(peer)
        peers: list[Peer] = []
        server = threading.Thread(target=refuse)
        server.start()
        c = MqttClient(HOST, self.server.port, "denied", username="amr", password="wrong")
        with self.assertRaises(ConnectRefused) as caught:
            c.connect(timeout=2.0)
        server.join(3.0)
        for peer in peers:
            peer.close()
        self.assertEqual(caught.exception.code, 5)
        self.assertIn("not authorized", str(caught.exception))
        self.assertFalse(c.connected)

    def test_no_connack_times_out(self):
        c = MqttClient(HOST, self.server.port, "slow")
        with self.assertRaises(mqtt_mod.MqttError):
            c.connect(timeout=0.3)                                # the listener never answers


if __name__ == "__main__":
    unittest.main()
