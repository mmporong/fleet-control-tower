"""MQTT 3.1.1 client on the standard library, for VDA 5050 over a real broker.

``fleet.bus`` keeps VDA 5050 inside one process. This module is the network
side: the fleet manager and each vehicle connect to an MQTT broker as separate
processes, the way they do on a site. It implements what VDA 5050 needs:

- CONNECT with a Last Will (the ``connection`` topic's CONNECTIONBROKEN), user
  name and password, and TLS through an ``ssl.SSLContext``;
- PUBLISH, SUBSCRIBE and UNSUBSCRIBE at QoS 0 and 1 (QoS 2 is not supported);
- a background network thread with keepalive PINGREQ, and automatic
  reconnection with exponential backoff that restores the subscriptions and
  resends QoS 1 messages the broker has not acknowledged, with DUP set;
- the packet codec, shared with ``fleet.minibroker``.

Callbacks run on the network thread. Keep them short; ``publish`` and
``subscribe`` called from a callback do not wait for acknowledgements.
"""
from __future__ import annotations

from collections import OrderedDict
import contextlib
from dataclasses import dataclass
import logging
import select
import socket
import ssl
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

PROTOCOL_NAME = "MQTT"
PROTOCOL_LEVEL = 4                 # MQTT 3.1.1
MAX_PACKET = 1024 * 1024           # bytes per packet, header included; larger ones are refused both ways
MAX_REMAINING = 268_435_455        # largest value the 4-byte remaining length can carry
IO_TIMEOUT_S = 10.0                # a blocked socket write longer than this counts as a lost connection

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14
PACKET_NAMES = {CONNECT: "CONNECT", CONNACK: "CONNACK", PUBLISH: "PUBLISH", PUBACK: "PUBACK",
                PUBREC: "PUBREC", PUBREL: "PUBREL", PUBCOMP: "PUBCOMP", SUBSCRIBE: "SUBSCRIBE",
                SUBACK: "SUBACK", UNSUBSCRIBE: "UNSUBSCRIBE", UNSUBACK: "UNSUBACK", PINGREQ: "PINGREQ",
                PINGRESP: "PINGRESP", DISCONNECT: "DISCONNECT"}
CONNACK_REASONS = {
    0: "accepted",
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorized",
}
SUBACK_FAILURE = 0x80


class MqttError(Exception):
    """Connection or protocol failure."""


class ProtocolError(MqttError):
    """The peer sent something MQTT 3.1.1 does not allow; the connection is closed."""


class PacketTooLarge(MqttError, ValueError):
    """A packet is over the size limit, on the way out or on the way in."""


class ConnectRefused(MqttError):
    """CONNACK carried a non-zero return code."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.reason = CONNACK_REASONS.get(code, "unknown return code")
        super().__init__(f"connection refused: {self.reason} ({code})")


@dataclass(frozen=True)
class Will:
    """Last Will: the broker publishes it when the client vanishes without DISCONNECT."""
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False


# ---- codec -------------------------------------------------------------------------------------------

def encode_remaining_length(n: int) -> bytes:
    """Variable-length integer of the fixed header, 1 to 4 bytes.

    >>> encode_remaining_length(0), encode_remaining_length(127), encode_remaining_length(128)
    (b'\\x00', b'\\x7f', b'\\x80\\x01')
    >>> encode_remaining_length(268_435_455).hex()
    'ffffff7f'
    """
    if not 0 <= n <= MAX_REMAINING:
        raise ValueError(f"remaining length {n} out of range")
    out = bytearray()
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | 0x80 if n else digit)
        if not n:
            return bytes(out)


def decode_remaining_length(data: bytes | bytearray, start: int = 1) -> tuple[int, int] | None:
    """(value, index after it), or None while bytes are still missing.

    >>> decode_remaining_length(b"\\x30\\x80\\x01")
    (128, 3)
    >>> decode_remaining_length(b"\\x30\\x80") is None
    True
    """
    value = 0
    for i in range(4):
        pos = start + i
        if pos >= len(data):
            return None
        byte = data[pos]
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value, pos + 1
    raise ProtocolError("malformed remaining length (more than 4 bytes)")


def take_packet(buf: bytearray, max_size: int = MAX_PACKET) -> tuple[int, int, bytes] | None:
    """Remove one complete packet from the front of ``buf`` and return (type, flags, body).

    Returns None until the whole packet has arrived. A packet over ``max_size``
    bytes is refused as soon as its header is readable, before its body is buffered.
    """
    if len(buf) < 2:
        return None
    decoded = decode_remaining_length(buf)
    if decoded is None:
        return None
    length, start = decoded
    end = start + length
    if end > max_size:
        raise PacketTooLarge(f"incoming packet of {end} bytes exceeds the {max_size} byte limit")
    if len(buf) < end:
        return None
    first = buf[0]
    body = bytes(buf[start:end])
    del buf[:end]
    return first >> 4, first & 0x0F, body


def packet(ptype: int, flags: int = 0, body: bytes = b"") -> bytes:
    return bytes([ptype << 4 | flags]) + encode_remaining_length(len(body)) + body


def encode_bytes(value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ValueError(f"field of {len(value)} bytes exceeds 65535")
    return len(value).to_bytes(2, "big") + value


def encode_str(value: str) -> bytes:
    """Length-prefixed UTF-8 string.

    >>> encode_str("AMR-01")
    b'\\x00\\x06AMR-01'
    """
    if "\x00" in value:
        raise ValueError("MQTT strings may not contain U+0000")
    return encode_bytes(value.encode("utf-8"))


def read_u16(data: bytes, pos: int) -> tuple[int, int]:
    if pos + 2 > len(data):
        raise ProtocolError("truncated packet")
    return int.from_bytes(data[pos:pos + 2], "big"), pos + 2


def read_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    n, pos = read_u16(data, pos)
    if pos + n > len(data):
        raise ProtocolError("truncated packet")
    return bytes(data[pos:pos + n]), pos + n


def read_str(data: bytes, pos: int) -> tuple[str, int]:
    raw, pos = read_bytes(data, pos)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("string is not valid UTF-8") from exc
    if "\x00" in text:
        raise ProtocolError("string contains U+0000")
    return text, pos


def check_topic_name(topic: str) -> None:
    """A topic to publish to: not empty, no wildcards."""
    if not topic:
        raise ValueError("empty topic name")
    if "+" in topic or "#" in topic:
        raise ValueError(f"wildcards are not allowed in a topic name: {topic!r}")
    encode_str(topic)


def check_topic_filter(topic_filter: str) -> None:
    """A subscription: '+' fills one whole level, '#' only as the whole last level."""
    if not topic_filter:
        raise ValueError("empty topic filter")
    levels = topic_filter.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            raise ValueError(f"'#' must be the whole last level: {topic_filter!r}")
        if "+" in level and level != "+":
            raise ValueError(f"'+' must be a whole level: {topic_filter!r}")
    encode_str(topic_filter)


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Subscription match, MQTT 3.1.1 section 4.7.

    >>> topic_matches("uagv/v2/+/+/connection", "uagv/v2/sim/AMR-01/connection")
    True
    >>> topic_matches("sport/#", "sport")        # '#' also matches the parent level
    True
    >>> topic_matches("+/monitor", "/monitor")   # '+' matches an empty level
    True
    >>> topic_matches("sport/+", "sport/a/b")
    False
    >>> topic_matches("#", "$SYS/broker/load")   # a leading wildcard skips '$' topics
    False
    >>> topic_matches("$SYS/#", "$SYS/broker/load")
    True
    """
    if topic.startswith("$") and topic_filter[:1] in ("+", "#"):
        return False
    f_levels = topic_filter.split("/")
    t_levels = topic.split("/")
    for i, level in enumerate(f_levels):
        if level == "#":
            return True
        if i >= len(t_levels) or (level != "+" and level != t_levels[i]):
            return False
    return len(f_levels) == len(t_levels)


def encode_connect(client_id: str, keepalive: int, clean_session: bool, will: Will | None = None,
                   username: str | None = None, password: str | bytes | None = None) -> bytes:
    if password is not None and username is None:
        raise ValueError("MQTT 3.1.1 sends a password only together with a user name")
    if not 0 <= keepalive <= 0xFFFF:
        raise ValueError("keepalive must be 0..65535 s")
    flags = 0x02 if clean_session else 0x00
    payload = encode_str(client_id)
    if will is not None:
        check_topic_name(will.topic)
        if will.qos not in (0, 1):
            raise ValueError("will qos must be 0 or 1")
        flags |= 0x04 | will.qos << 3 | (0x20 if will.retain else 0)
        payload += encode_str(will.topic) + encode_bytes(bytes(will.payload))
    if username is not None:
        flags |= 0x80
        payload += encode_str(username)
    if password is not None:
        flags |= 0x40
        payload += encode_bytes(password.encode("utf-8") if isinstance(password, str) else bytes(password))
    body = encode_str(PROTOCOL_NAME) + bytes([PROTOCOL_LEVEL, flags]) + keepalive.to_bytes(2, "big") + payload
    return packet(CONNECT, 0, body)


def encode_publish(topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                   packet_id: int = 0, dup: bool = False) -> bytes:
    flags = (0x08 if dup else 0) | qos << 1 | (0x01 if retain else 0)
    body = encode_str(topic) + (packet_id.to_bytes(2, "big") if qos else b"") + bytes(payload)
    return packet(PUBLISH, flags, body)


def parse_publish(flags: int, body: bytes) -> tuple[str, bytes, int, bool, bool, int]:
    """(topic, payload, qos, retain, dup, packet_id) of a PUBLISH."""
    qos = flags >> 1 & 0x03
    if qos == 3:
        raise ProtocolError("PUBLISH with QoS 3")
    topic, pos = read_str(body, 0)
    packet_id = 0
    if qos:
        packet_id, pos = read_u16(body, pos)
        if packet_id == 0:
            raise ProtocolError("PUBLISH with packet identifier 0")
    return topic, body[pos:], qos, bool(flags & 0x01), bool(flags & 0x08), packet_id


def encode_subscribe(packet_id: int, requests: list[tuple[str, int]]) -> bytes:
    body = packet_id.to_bytes(2, "big") + b"".join(encode_str(f) + bytes([q]) for f, q in requests)
    return packet(SUBSCRIBE, 0x02, body)


def encode_unsubscribe(packet_id: int, filters: list[str]) -> bytes:
    return packet(UNSUBSCRIBE, 0x02, packet_id.to_bytes(2, "big") + b"".join(encode_str(f) for f in filters))


PINGREQ_PACKET = packet(PINGREQ)
PINGRESP_PACKET = packet(PINGRESP)
DISCONNECT_PACKET = packet(DISCONNECT)


def wait_readable(sock: socket.socket, timeout: float | None) -> bool:
    """True when the descriptor has input (or EOF/error) within ``timeout`` s; poll() has no fd limit.

    Bytes TLS has already decrypted are not visible here; check ``SSLSocket.pending()`` first.
    """
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return bool(poller.poll(None if timeout is None else max(0, int(timeout * 1000)) or 1))
    readable, _, _ = select.select([sock], [], [], timeout)
    return bool(readable)


# ---- client ------------------------------------------------------------------------------------------

class _Waiter:
    """One pending acknowledgement (PUBACK, SUBACK or UNSUBACK)."""
    __slots__ = ("kind", "event", "result")

    def __init__(self, kind: int) -> None:
        self.kind = kind
        self.event = threading.Event()
        self.result: object = None

    def done(self, result: object) -> None:
        self.result = result
        self.event.set()


class MqttClient:
    """One MQTT 3.1.1 connection with a background network thread.

    ::

        c = MqttClient("broker", 1883, "fms", will=Will(topic, b"...", 1, True))
        c.on_message = handle              # (topic, payload, retain), on the network thread
        c.connect()                        # raises ConnectRefused, MqttError or OSError
        c.start()                          # receive, keepalive, reconnect
        c.subscribe("uagv/v2/+/+/state")
        c.publish(topic, payload, qos=1)
        c.disconnect()                     # clean: the broker drops the Will
        c.stop()

    ``start()`` without ``connect()`` lets the thread make the first connection
    itself, retrying until the broker is there. ``stop()`` alone closes the
    socket without DISCONNECT, which the broker treats like a crash (Will sent).
    """

    def __init__(self, host: str, port: int, client_id: str, keepalive: int = 15,
                 clean_session: bool = True, username: str | None = None,
                 password: str | bytes | None = None, tls: ssl.SSLContext | None = None,
                 will: Will | None = None, *, max_packet: int = MAX_PACKET,
                 reconnect_delay: tuple[float, float] = (1.0, 30.0), max_inflight: int = 1000) -> None:
        if not clean_session and not client_id:
            raise ValueError("a persistent session (clean_session=False) needs a client id")
        # built once, so bad settings fail here and not on some later reconnect
        self._connect_packet = encode_connect(client_id, keepalive, clean_session, will, username, password)
        self.host = host
        self.port = port
        self.client_id = client_id
        self.keepalive = keepalive
        self.clean_session = clean_session
        self.will = will
        self.tls = tls
        self.max_packet = max_packet
        self.reconnect_delay = reconnect_delay
        self.max_inflight = max_inflight
        self.session_present = False
        self.on_message: Callable[[str, bytes, bool], None] | None = None
        self.on_connect: Callable[[], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None

        self._sock: socket.socket | None = None
        self._rbuf = bytearray()
        self._write_lock = threading.Lock()       # one writer on the socket at a time (TLS: one user at a time)
        self._state_lock = threading.Lock()       # subscriptions, in-flight messages, waiters, packet ids
        self._conn_lock = threading.Lock()        # one connection attempt at a time
        self._up = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._subs: dict[str, int] = {}
        self._inflight: OrderedDict[int, list] = OrderedDict()    # packet id -> [packet, sent once]
        self._waiters: dict[int, _Waiter] = {}
        self._last_id = 0
        self._want = False                        # keep (re)connecting
        self._delay = 0.0                         # wait before the next connection attempt
        self._connect_timeout = 5.0
        self._last_send = self._last_recv = 0.0
        self._ping_at: float | None = None
        self._tick = 0.5 if keepalive == 0 else min(0.5, keepalive / 4)

    # -- state

    @property
    def connected(self) -> bool:
        return self._sock is not None

    @property
    def inflight(self) -> int:
        """QoS 1 messages not yet acknowledged by the broker."""
        with self._state_lock:
            return len(self._inflight)

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self._up.wait(timeout)

    # -- connection

    def connect(self, timeout: float = 5.0) -> None:
        """Open the connection and wait for CONNACK. Raises ConnectRefused, MqttError or OSError."""
        self._connect_timeout = timeout
        self._want = True
        self._open(timeout)

    def disconnect(self, timeout: float = 1.0) -> None:
        """Send DISCONNECT and close; the broker discards the Will. No reconnection until connect()/start()."""
        self._want = False
        sock = self._sock
        if sock is not None:
            self._drop(sock, "disconnect requested", graceful=timeout)

    def start(self) -> None:
        """Start the network thread; it makes the first connection itself if connect() was not called."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._halt = threading.Event()        # per thread: a straggler from an earlier stop() stays stopped
        self._want = True
        self._delay = 0.0
        self._thread = threading.Thread(target=self._run, args=(self._halt,), name=f"mqtt-{self.client_id}",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the network thread. An open socket is closed without DISCONNECT, so the Will goes out."""
        self._want = False
        self._halt.set()
        sock = self._sock
        if sock is not None:
            self._drop(sock, "client stopped")
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    # -- messaging

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                timeout: float | None = None) -> bool:
        """Send one message.

        QoS 0: True when written to the socket; False when offline (dropped).
        QoS 1: kept until PUBACK and resent with DUP after a reconnect. True once
        queued; False when ``max_inflight`` messages are already waiting. With
        ``timeout`` it waits for the PUBACK and returns False if none came
        (the message stays queued).
        """
        check_topic_name(topic)
        if qos not in (0, 1):
            raise ValueError("qos must be 0 or 1 (QoS 2 is not supported)")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes")
        if qos == 0:
            return self._send(self._sized(encode_publish(topic, payload, 0, retain)))
        waiter = None
        with self._state_lock:
            if len(self._inflight) >= self.max_inflight:
                return False
            mid = self._new_id()
            entry = [self._sized(encode_publish(topic, payload, 1, retain, mid)), False]
            self._inflight[mid] = entry
            if timeout is not None and self._can_wait():
                waiter = self._waiters[mid] = _Waiter(PUBACK)
        if self._send(entry[0]):
            entry[1] = True
        if waiter is None:
            return True
        acked = waiter.event.wait(timeout)
        with self._state_lock:
            self._waiters.pop(mid, None)
        return acked

    def subscribe(self, topic_filter: str, qos: int = 0, timeout: float | None = 5.0) -> int | None:
        """Subscribe now and again after every reconnect.

        Returns the QoS granted in SUBACK (needs the network thread), or None when
        offline or not confirmed within ``timeout``. Raises MqttError if refused.
        """
        check_topic_filter(topic_filter)
        if qos not in (0, 1):
            raise ValueError("qos must be 0 or 1 (QoS 2 is not supported)")
        with self._state_lock:
            self._subs[topic_filter] = qos
        codes = self._request(SUBACK, encode_subscribe, [(topic_filter, qos)], timeout)
        if not codes:
            return None
        if codes[0] == SUBACK_FAILURE:
            with self._state_lock:
                self._subs.pop(topic_filter, None)
            raise MqttError(f"broker refused the subscription to {topic_filter!r}")
        return codes[0]

    def unsubscribe(self, topic_filter: str, timeout: float | None = 5.0) -> bool:
        """Forget a subscription; True when the broker confirmed it with UNSUBACK."""
        with self._state_lock:
            self._subs.pop(topic_filter, None)
        return self._request(UNSUBACK, encode_unsubscribe, [topic_filter], timeout) is not None

    # -- internals

    def _sized(self, data: bytes) -> bytes:
        if len(data) > self.max_packet:
            raise PacketTooLarge(f"packet of {len(data)} bytes exceeds the {self.max_packet} byte limit")
        return data

    def _new_id(self) -> int:
        """Next free packet identifier; call with the state lock held."""
        for _ in range(0xFFFF):
            self._last_id = self._last_id % 0xFFFF + 1
            if self._last_id not in self._inflight and self._last_id not in self._waiters:
                return self._last_id
        raise MqttError("no free packet identifier")

    def _can_wait(self) -> bool:
        """Acknowledgements arrive only if the network thread runs and is not the caller."""
        thread = self._thread
        return thread is not None and thread.is_alive() and thread is not threading.current_thread()

    def _request(self, kind: int, encode: Callable, items: list, timeout: float | None) -> list[int] | None:
        """Send a SUBSCRIBE or UNSUBSCRIBE; return the acknowledgement's codes, or None."""
        with self._state_lock:
            if self._sock is None:
                return None
            mid = self._new_id()
            waiter = self._waiters[mid] = _Waiter(kind)
        if not self._send(encode(mid, items)):
            with self._state_lock:
                self._waiters.pop(mid, None)
            return None
        if timeout is None or not self._can_wait():
            return None               # the waiter is cleared when the acknowledgement arrives
        if not waiter.event.wait(timeout):
            with self._state_lock:
                self._waiters.pop(mid, None)
            return None
        return waiter.result          # None if the connection dropped meanwhile

    def _open(self, timeout: float) -> None:
        with self._conn_lock:
            if self._sock is not None:
                return
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if self.tls is not None:
                    sock = self.tls.wrap_socket(sock, server_hostname=self.host)
                sock.sendall(self._connect_packet)
                buf = bytearray()
                self._read_connack(sock, buf, timeout)
                sock.settimeout(IO_TIMEOUT_S)
            except BaseException:
                sock.close()
                raise
            self._rbuf = buf
            self._last_send = self._last_recv = time.monotonic()
            self._ping_at = None
            with self._state_lock:
                self._sock = sock
                subs = list(self._subs.items())
                resend = []
                for entry in self._inflight.values():
                    if entry[1]:
                        entry[0] = bytes([entry[0][0] | 0x08]) + entry[0][1:]     # DUP: sent before
                    entry[1] = True
                    resend.append(entry[0])
                sub_ids = []
                for _ in subs:
                    mid = self._new_id()
                    self._waiters[mid] = _Waiter(SUBACK)      # keeps the id reserved until SUBACK
                    sub_ids.append(mid)
            self._up.set()
        log.info("mqtt %s: connected to %s:%d (session present: %s)",
                 self.client_id, self.host, self.port, self.session_present)
        # subscriptions first, so a resent message on a subscribed topic comes back to this client too
        for mid, request in zip(sub_ids, subs):
            self._send(encode_subscribe(mid, [request]))
        for data in resend:
            self._send(data)
        if self._sock is sock:
            self._callback(self.on_connect)

    def _read_connack(self, sock: socket.socket, buf: bytearray, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while (pkt := take_packet(buf, self.max_packet)) is None:
            left = deadline - time.monotonic()
            if left <= 0 or not (self._pending(sock) or wait_readable(sock, left)):
                raise MqttError(f"no CONNACK within {timeout:g} s")
            data = sock.recv(65536)
            if not data:
                raise MqttError("connection closed before CONNACK")
            buf += data
        ptype, _, body = pkt
        if ptype != CONNACK or len(body) != 2:
            raise ProtocolError(f"expected CONNACK, got {PACKET_NAMES.get(ptype, ptype)}")
        if body[1]:
            raise ConnectRefused(body[1])
        self.session_present = bool(body[0] & 0x01)

    def _run(self, halt: threading.Event) -> None:
        try:
            while not halt.is_set():
                sock = self._sock
                if sock is None:
                    self._reconnect_step(halt)
                    continue
                try:
                    self._service(sock)
                except (OSError, ValueError, MqttError) as exc:
                    self._drop(sock, str(exc) or type(exc).__name__)
        finally:
            sock = self._sock
            if sock is not None:
                self._drop(sock, "client stopped")

    def _reconnect_step(self, halt: threading.Event) -> None:
        if not self._want:
            halt.wait(self._tick)
            return
        if self._delay and halt.wait(self._delay):
            return
        if halt.is_set() or not self._want or self._sock is not None:
            return
        try:
            self._open(self._connect_timeout)
        except (OSError, MqttError) as exc:
            low, high = self.reconnect_delay
            self._delay = min(max(self._delay * 2, low), high)
            # a refused CONNECT (credentials, client id) is a configuration error that retrying will not fix
            level = logging.WARNING if isinstance(exc, ConnectRefused) else logging.INFO
            log.log(level, "mqtt %s: connection attempt failed (%s); next in %.1f s", self.client_id, exc,
                    self._delay)

    def _tls_guard(self):
        # OpenSSL objects are not safe for a concurrent read and write, and SSLSocket.close() swaps out
        # the object other calls use, so every TLS call takes the write lock
        return self._write_lock if self.tls is not None else contextlib.nullcontext()

    def _pending(self, sock: socket.socket) -> bool:
        """Decrypted TLS bytes waiting inside the SSL object (poll() cannot see them)."""
        return self.tls is not None and sock.pending() > 0

    def _service(self, sock: socket.socket) -> None:
        with self._tls_guard():
            pending = self._pending(sock)
        if pending or wait_readable(sock, self._tick):
            with self._tls_guard():
                data = sock.recv(65536)
            if not data:
                raise MqttError("connection closed by broker")
            if self._sock is not sock:
                return                  # retired while reading; its bytes must not reach a newer connection
            self._last_recv = time.monotonic()
            self._rbuf += data
            while self._sock is sock and (pkt := take_packet(self._rbuf, self.max_packet)) is not None:
                self._handle(*pkt)
        self._keepalive()

    def _keepalive(self) -> None:
        if not self.keepalive:
            return
        now = time.monotonic()
        if self._ping_at is not None:
            if now - self._ping_at > self.keepalive:
                raise MqttError("keepalive timeout: no PINGRESP")
        elif now - min(self._last_send, self._last_recv) >= 0.75 * self.keepalive:
            self._ping_at = now
            self._send(PINGREQ_PACKET)

    def _handle(self, ptype: int, flags: int, body: bytes) -> None:
        if ptype == PUBLISH:
            topic, payload, qos, retain, _dup, mid = parse_publish(flags, body)
            if qos > 1:
                raise ProtocolError("QoS 2 delivery is not supported")
            self._callback(self.on_message, topic, payload, retain)
            if qos == 1:
                self._send(packet(PUBACK, 0, mid.to_bytes(2, "big")))
        elif ptype == PUBACK:
            mid, _ = read_u16(body, 0)
            with self._state_lock:
                self._inflight.pop(mid, None)
                waiter = self._waiters.pop(mid, None)
            if waiter is not None:
                waiter.done(True)
        elif ptype in (SUBACK, UNSUBACK):
            mid, pos = read_u16(body, 0)
            with self._state_lock:
                waiter = self._waiters.pop(mid, None)
            codes = list(body[pos:])
            if SUBACK_FAILURE in codes:
                log.warning("mqtt %s: broker refused a subscription (SUBACK %s)", self.client_id, codes)
            if waiter is not None:
                waiter.done(codes)
        elif ptype == PINGRESP:
            self._ping_at = None
        else:
            raise ProtocolError(f"unexpected {PACKET_NAMES.get(ptype, ptype)} from broker")

    def _send(self, data: bytes) -> bool:
        with self._write_lock:
            sock = self._sock
            if sock is None:
                return False
            try:
                sock.sendall(data)
            except OSError as exc:
                error = exc
            else:
                self._last_send = time.monotonic()
                return True
        self._drop(sock, f"send failed: {error}")
        return False

    def _drop(self, sock: socket.socket, reason: str, graceful: float | None = None) -> None:
        """Retire ``sock`` once: close it, release waiters, report the reason."""
        with self._state_lock:
            if self._sock is not sock:
                return
            self._sock = None
            self._up.clear()
            self._ping_at = None
            self._delay = self.reconnect_delay[0]
            stale = [mid for mid, w in self._waiters.items() if w.kind != PUBACK]
            released = [self._waiters.pop(mid) for mid in stale]
        if graceful is not None:
            self._close_gracefully(sock, graceful)
        # shut the descriptor down first (the plain-socket method leaves an SSLSocket's state alone), which
        # wakes a thread blocked on it; then close under the TLS guard so no SSL call is mid-flight
        with contextlib.suppress(OSError):
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        with self._tls_guard():
            sock.close()
        for waiter in released:
            waiter.done(None)
        log.info("mqtt %s: disconnected (%s)", self.client_id, reason)
        self._callback(self.on_disconnect, reason)

    def _close_gracefully(self, sock: socket.socket, timeout: float) -> None:
        # DISCONNECT, then half-close and wait for the broker's close: closing with unread input
        # would send a reset that can destroy the DISCONNECT before the broker reads it
        with self._write_lock:
            try:
                sock.sendall(DISCONNECT_PACKET)
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                return
        deadline = time.monotonic() + timeout
        try:
            while (left := deadline - time.monotonic()) > 0 and wait_readable(sock, left):
                with self._tls_guard():
                    if not sock.recv(65536):
                        return
        except (OSError, ValueError):
            pass

    def _callback(self, fn: Callable | None, *args) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            log.exception("mqtt %s: callback %s failed", self.client_id, getattr(fn, "__name__", fn))
