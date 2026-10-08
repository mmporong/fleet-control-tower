"""Minimal MQTT 3.1.1 broker for tests and local development. Not for production.

It lets the fleet manager, the vehicles and the test suite talk MQTT without
installing a broker, and covers what ``fleet.mqtt`` and VDA 5050 use:

- CONNECT with a Last Will, and takeover when a client id connects again
  (the old connection is closed; its Will is dropped, since the same client
  is back);
- SUBSCRIBE / UNSUBSCRIBE with '+' and '#', PUBLISH at QoS 0 and 1, delivered
  at min(publish QoS, subscription QoS);
- retained messages (an empty retained payload deletes one), sent to new
  subscribers with the retain flag;
- keepalive supervision: silence for 1.5 x keepalive closes the connection and
  publishes the Will, as does a socket closed without DISCONNECT.

Deliberately missing: authentication and ACLs (every CONNECT is accepted), TLS,
QoS 2, persistent sessions (CleanSession=0 is accepted but nothing is kept once
the connection ends), QoS 1 retransmission to subscribers, $SYS topics and
retained messages that survive a restart. On a site, use mosquitto or another
production broker.

Run it on its own::

    python3 -m fleet.minibroker --port 1883
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import itertools
import logging
import signal
import socket
import threading
import time
import uuid

from .mqtt import (CONNACK, CONNECT, DISCONNECT, MAX_PACKET, PACKET_NAMES, PINGREQ, PINGRESP_PACKET,
                   PROTOCOL_LEVEL, PROTOCOL_NAME, PUBACK, PUBLISH, SUBACK, SUBACK_FAILURE, SUBSCRIBE,
                   UNSUBACK, UNSUBSCRIBE, MqttError, ProtocolError, Will, check_topic_filter, check_topic_name,
                   encode_publish, packet, parse_publish, read_bytes, read_str, read_u16, take_packet,
                   topic_matches, wait_readable)

log = logging.getLogger("fleet.minibroker")      # same name when run with -m

CONNECT_WAIT_S = 10.0       # a socket that sends no CONNECT within this is closed
SEND_TIMEOUT_S = 5.0        # a client that takes no data for this long is disconnected
KEEPALIVE_GRACE = 1.5       # MQTT 3.1.1 section 3.1.2.10


class _Expired(Exception):
    pass


class _Conn:
    """One client connection, served by its own thread."""

    def __init__(self, sock: socket.socket, addr) -> None:
        self.sock = sock
        self.addr = addr
        self.client_id = ""
        self.keepalive = 0
        self.will: Will | None = None
        self.subs: dict[str, int] = {}
        self.write_lock = threading.Lock()
        self.ids = itertools.cycle(range(1, 0x10000))
        self.thread: threading.Thread | None = None

    def send(self, data: bytes) -> bool:
        with self.write_lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                pass
        self.kick()
        return False

    def send_publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.send(encode_publish(topic, payload, qos, retain, next(self.ids) if qos else 0))

    def kick(self) -> None:
        """Wake the serving thread with EOF; it closes the socket and cleans up."""
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)


class MiniBroker:
    """Threaded MQTT 3.1.1 broker: one thread per connection, state under one lock.

    Deliveries are written while that lock is held, so every subscriber sees
    messages in one global order (a retained message and a live update cannot
    swap). A subscriber that blocks a write for ``SEND_TIMEOUT_S`` is dropped.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, max_packet: int = MAX_PACKET) -> None:
        self.host = host
        self.port = port
        self.max_packet = max_packet
        self.stats: Counter[str] = Counter()             # packets received, by type name
        self._lock = threading.RLock()
        self._clients: dict[str, _Conn] = {}              # connected sessions by client id
        self._conns: set[_Conn] = set()                   # every open socket, CONNECT or not
        self._retained: dict[str, tuple[bytes, int]] = {}
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle

    def start(self) -> MiniBroker:
        self._listener = socket.create_server((self.host, self.port))      # SO_REUSEADDR on POSIX
        self.port = self._listener.getsockname()[1]
        self._stopping = False
        self._accept_thread = threading.Thread(target=self._accept_loop, name=f"minibroker-{self.port}",
                                               daemon=True)
        self._accept_thread.start()
        log.info("minibroker listening on %s:%d", self.host, self.port)
        return self

    def stop(self) -> None:
        """Close the listener and every connection (no Wills are published). Safe to call twice."""
        with self._lock:
            self._stopping = True
            conns = list(self._conns)
        if self._accept_thread is not None:
            self._accept_thread.join(2.0)
            self._accept_thread = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        for c in conns:
            c.kick()
        for c in conns:
            if c.thread is not None:
                c.thread.join(2.0)

    def client_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._clients)

    def retained(self) -> dict[str, bytes]:
        with self._lock:
            return {t: payload for t, (payload, _) in self._retained.items()}

    # -- connections

    def _accept_loop(self) -> None:
        listener = self._listener
        listener.settimeout(0.2)             # how quickly stop() is noticed
        while not self._stopping:
            try:
                sock, addr = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stopping:
                    break
                log.warning("minibroker: accept failed: %s", exc)
                time.sleep(0.05)
                continue
            conn = _Conn(sock, addr)
            with self._lock:
                if self._stopping:
                    sock.close()
                    break
                self._conns.add(conn)
                conn.thread = threading.Thread(target=self._serve, args=(conn,),
                                               name=f"minibroker-conn-{addr[1]}", daemon=True)
                conn.thread.start()

    def _read(self, c: _Conn, buf: bytearray, limit: float | None) -> tuple[int, int, bytes]:
        deadline = None if limit is None else time.monotonic() + limit
        while (pkt := take_packet(buf, self.max_packet)) is None:
            # wait here, not in recv: the socket's own timeout is the send timeout
            left = None if deadline is None else deadline - time.monotonic()
            if (left is not None and left <= 0) or not wait_readable(c.sock, left):
                raise _Expired()
            data = c.sock.recv(65536)
            if not data:
                raise ConnectionError("socket closed without DISCONNECT")
            buf += data
        with self._lock:
            self.stats[PACKET_NAMES.get(pkt[0], str(pkt[0]))] += 1
        return pkt

    def _serve(self, c: _Conn) -> None:
        buf = bytearray()
        clean = False
        try:
            c.sock.settimeout(SEND_TIMEOUT_S)
            ptype, _, body = self._read(c, buf, CONNECT_WAIT_S)
            if ptype != CONNECT:
                raise ProtocolError("first packet must be CONNECT")
            if not self._accept_connect(c, body):
                return
            limit = c.keepalive * KEEPALIVE_GRACE if c.keepalive else None
            while True:
                ptype, flags, body = self._read(c, buf, limit)
                if ptype == DISCONNECT:
                    clean = True
                    return
                self._dispatch(c, ptype, flags, body)
        except _Expired:
            log.info("minibroker: %s silent past %.1f x keepalive, closing", c.client_id or c.addr,
                     KEEPALIVE_GRACE)
        except (OSError, ValueError, MqttError) as exc:
            log.debug("minibroker: %s: %s", c.client_id or c.addr, exc)
        finally:
            self._finish(c, clean)

    def _accept_connect(self, c: _Conn, body: bytes) -> bool:
        name, pos = read_str(body, 0)
        if pos + 4 > len(body):
            raise ProtocolError("truncated CONNECT")
        level, flags = body[pos], body[pos + 1]
        keepalive, pos = read_u16(body, pos + 2)
        if name != PROTOCOL_NAME or level != PROTOCOL_LEVEL:
            if name in ("MQTT", "MQIsdp"):          # a known protocol in a version this broker does not speak
                c.send(packet(CONNACK, 0, b"\x00\x01"))
            return False
        if flags & 0x01:
            raise ProtocolError("reserved CONNECT flag set")
        clean_session = bool(flags & 0x02)
        client_id, pos = read_str(body, pos)
        will = None
        if flags & 0x04:
            will_topic, pos = read_str(body, pos)
            will_payload, pos = read_bytes(body, pos)
            will_qos = flags >> 3 & 0x03
            if will_qos == 3:
                raise ProtocolError("will QoS 3")
            try:
                check_topic_name(will_topic)
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
            will = Will(will_topic, will_payload, min(will_qos, 1), bool(flags & 0x20))
        elif flags & 0x38:
            raise ProtocolError("will QoS or retain set without a will")
        if flags & 0x80:
            _, pos = read_str(body, pos)             # user name: accepted, not checked
        if flags & 0x40:
            _, pos = read_bytes(body, pos)           # password: accepted, not checked
        if not client_id:
            if not clean_session:
                c.send(packet(CONNACK, 0, b"\x00\x02"))
                return False
            client_id = f"minibroker-{uuid.uuid4().hex[:12]}"
        with self._lock:
            if self._stopping:
                return False
            old = self._clients.get(client_id)
            if old is not None:
                old.will = None                       # the same client is back: its old link is not a crash
            self._clients[client_id] = c
            c.client_id, c.keepalive, c.will = client_id, keepalive, will
            c.send(packet(CONNACK, 0, b"\x00\x00"))  # under the lock: nothing can be routed to c before it
        if old is not None:
            log.info("minibroker: %s connected again, closing its previous connection", client_id)
            old.kick()
        log.info("minibroker: %s connected from %s (keepalive %d s, will %s)", client_id, c.addr, keepalive,
                 will.topic if will else "none")
        return True

    def _dispatch(self, c: _Conn, ptype: int, flags: int, body: bytes) -> None:
        if ptype == PUBLISH:
            topic, payload, qos, retain, _dup, mid = parse_publish(flags, body)
            if qos == 2:
                raise ProtocolError("QoS 2 is not supported by this broker")
            try:
                check_topic_name(topic)
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
            self._route(topic, payload, qos, retain)
            if qos == 1:
                c.send(packet(PUBACK, 0, mid.to_bytes(2, "big")))
        elif ptype == PUBACK:
            pass                                      # no retransmission state is kept for subscribers
        elif ptype == SUBSCRIBE:
            mid, requests = self._parse_filters(flags, body, with_qos=True)
            self._subscribe(c, mid, requests)
        elif ptype == UNSUBSCRIBE:
            mid, requests = self._parse_filters(flags, body, with_qos=False)
            with self._lock:
                for topic_filter, _ in requests:
                    c.subs.pop(topic_filter, None)
            c.send(packet(UNSUBACK, 0, mid.to_bytes(2, "big")))
        elif ptype == PINGREQ:
            c.send(PINGRESP_PACKET)
        else:
            raise ProtocolError(f"unexpected {PACKET_NAMES.get(ptype, ptype)} from client")

    @staticmethod
    def _parse_filters(flags: int, body: bytes, with_qos: bool) -> tuple[int, list[tuple[str, int]]]:
        if flags != 0x02:
            raise ProtocolError("SUBSCRIBE/UNSUBSCRIBE fixed header flags must be 0010")
        mid, pos = read_u16(body, 0)
        requests = []
        while pos < len(body):
            topic_filter, pos = read_str(body, pos)
            qos = 0
            if with_qos:
                if pos >= len(body):
                    raise ProtocolError("truncated SUBSCRIBE")
                qos = body[pos]
                pos += 1
                if qos > 2:
                    raise ProtocolError(f"requested QoS {qos}")
            requests.append((topic_filter, qos))
        if not requests:
            raise ProtocolError("no topic filters")
        return mid, requests

    def _subscribe(self, c: _Conn, mid: int, requests: list[tuple[str, int]]) -> None:
        with self._lock:
            codes = []
            retained = []
            for topic_filter, qos in requests:
                try:
                    check_topic_filter(topic_filter)
                except ValueError:
                    codes.append(SUBACK_FAILURE)
                    continue
                granted = min(qos, 1)
                c.subs[topic_filter] = granted
                codes.append(granted)
                retained += [(t, payload, min(rq, granted)) for t, (payload, rq) in self._retained.items()
                             if topic_matches(topic_filter, t)]
            c.send(packet(SUBACK, 0, mid.to_bytes(2, "big") + bytes(codes)))
            for t, payload, qos in retained:
                c.send_publish(t, payload, qos, retain=True)

    def _route(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        with self._lock:
            if retain:
                if payload:
                    self._retained[topic] = (bytes(payload), qos)
                else:
                    self._retained.pop(topic, None)       # empty retained payload: delete, forward as usual
            for c in list(self._clients.values()):
                granted = max((q for f, q in c.subs.items() if topic_matches(f, topic)), default=-1)
                if granted >= 0:
                    c.send_publish(topic, payload, min(qos, granted), retain=False)

    def _finish(self, c: _Conn, clean: bool) -> None:
        with self._lock:
            self._conns.discard(c)
            if c.client_id and self._clients.get(c.client_id) is c:
                del self._clients[c.client_id]
            will, c.will = c.will, None
            if will is not None and not clean and not self._stopping:
                # still under the lock: once the client id is gone, its Will has been delivered
                log.info("minibroker: %s lost without DISCONNECT, publishing will on %s", c.client_id, will.topic)
                self._route(will.topic, will.payload, will.qos, will.retain)
        with contextlib.suppress(OSError):
            c.sock.shutdown(socket.SHUT_RDWR)
        c.sock.close()
        if clean:
            log.info("minibroker: %s disconnected", c.client_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Minimal MQTT 3.1.1 broker for tests and local development "
                                             "(not for production).")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("-v", "--verbose", action="store_true", help="log every connection detail")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    broker = MiniBroker(args.host, args.port).start()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        broker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
