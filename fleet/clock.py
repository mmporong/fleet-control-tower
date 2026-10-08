"""Time bases for VDA 5050 timestamps.

The fleet manager works in seconds since its own start (``now``). Messages carry
ISO-8601 UTC timestamps. A clock converts between the two:

- ``SimClock``: simulation seconds on a fixed shift calendar, so a seeded run
  produces the same messages every time;
- ``WallClock``: real time for the MQTT runtime, where vehicles and the manager
  are separate processes (clocks are assumed NTP-synchronised, as VDA 5050 does).

Latency measured by the manager is receive time minus the parsed send timestamp,
both on the same base.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time

SHIFT_START = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()


def format_utc(epoch_s: float) -> str:
    """ISO-8601 UTC with milliseconds and a Z suffix, the form VDA 5050 uses.

    >>> format_utc(0.25)
    '1970-01-01T00:00:00.250Z'
    """
    ms = int(round(epoch_s * 1000))
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def parse_utc(stamp: str) -> float | None:
    """Epoch seconds of an ISO-8601 timestamp; None when it cannot be read.

    >>> parse_utc("1970-01-01T00:00:01.5Z")
    1.5
    >>> round(parse_utc("2017-04-15T11:40:03.12Z") - parse_utc("2017-04-15T11:40:03Z"), 3)
    0.12
    >>> parse_utc("yesterday") is None
    True
    """
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp(), 6)


class Clock:
    """Seconds since ``base`` (an epoch time) <-> ISO-8601 stamps."""

    def __init__(self, base_epoch: float) -> None:
        self.base = base_epoch

    def iso(self, t: float) -> str:
        return format_utc(self.base + t)

    def parse(self, stamp: str) -> float | None:
        epoch = parse_utc(stamp)
        return None if epoch is None else round(epoch - self.base, 6)


class SimClock(Clock):
    """Simulation time on a fixed calendar starting 2026-10-01 00:00 UTC.

    >>> c = SimClock()
    >>> c.iso(3723.456)
    '2026-10-01T01:02:03.456Z'
    >>> c.parse(c.iso(90000.0))
    90000.0
    """

    def __init__(self) -> None:
        super().__init__(SHIFT_START)


class WallClock(Clock):
    """Real time: ``now()`` is monotonic seconds since construction, stamps are wall-clock UTC."""

    def __init__(self) -> None:
        self._mono0 = time.monotonic()
        super().__init__(time.time())

    def now(self) -> float:
        return time.monotonic() - self._mono0
