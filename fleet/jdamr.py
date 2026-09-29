"""Read-only link to the real JD-AMR through its existing AMMR dashboard API.

The AMMR dashboard (robot-dashboard, projects/ammr-live) already exposes the
Pi's navigation, battery and motion state at ``/api/state`` on the laptop. This
adapter polls that endpoint and summarises it for the control room. It never
sends commands: the real robot appears as an observe-only unit on its own map,
the same role Open-RMF gives a read-only fleet.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

DEFAULT_URL = "http://127.0.0.1:8090/api/state"

KO = {"SUCCEEDED": "도착", "EXECUTING": "주행", "ACCEPTED": "수락", "ABORTED": "중단", "CANCELED": "취소",
      "CANCELING": "취소 중", "IDLE": "대기", "STOP": "정지", "SLOWDOWN": "감속", "APPROACH": "접근 감속",
      "LIMIT": "속도 제한", "DO_NOTHING": "정상", "UNKNOWN": "알 수 없음"}


def _loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields a control room needs from the AMMR dashboard payload."""
    nav = state.get("navigation") or {}
    execution = nav.get("execution") or {}
    goal = nav.get("goal") or {}
    collision = nav.get("collision") or {}
    loc = nav.get("localization") or {}
    battery = state.get("battery") or {}
    odom = (state.get("motion") or {}).get("odom") or {}

    def live(item: dict) -> bool:
        return bool(item) and bool(item.get("live"))

    drive = None
    if live(execution) and execution.get("state"):
        drive = str(execution["state"])
    elif live(goal) and goal.get("status"):
        drive = str(goal["status"])
    blockers = [str(b) for b in execution.get("blockers") or []] if live(execution) else []
    if not blockers and live(collision) and collision.get("action"):
        action = str(collision["action"])
        if action not in ("DO_NOTHING", "UNKNOWN"):
            blockers = [f"{KO.get(action, action)}" + (f" · 영역 {collision.get('polygon')}" if collision.get("polygon") else "")]
    return {
        "drive": KO.get(drive, drive) if drive else None,
        "blockers": blockers,
        "battery_pct": battery.get("percentage_pct") if live(battery) else None,
        "battery_v": battery.get("voltage_v") if live(battery) else None,
        "speed_mps": odom.get("linear_mps") if live(odom) else None,
        "pose": ({"x": loc.get("x_m"), "y": loc.get("y_m"), "yaw_deg": loc.get("yaw_deg")}
                 if live(loc) else None),
        "uptime_s": (state.get("server") or {}).get("uptime_s"),
    }


class JdamrReadOnly(threading.Thread):
    def __init__(self, url: str = DEFAULT_URL, period_s: float = 1.0, timeout_s: float = 1.5) -> None:
        super().__init__(daemon=True, name="jdamr-readonly")
        if not _loopback(url):
            raise ValueError("JD-AMR adapter only reads a loopback URL (the local AMMR dashboard or its SSH tunnel)")
        self.url = url
        self.period_s = period_s
        self.timeout_s = timeout_s
        self._opener = build_opener(ProxyHandler({}))   # never route loopback reads through a proxy
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._status: dict[str, Any] = {"url": url, "connected": False, "error": "아직 조회 전", "data": None,
                                        "last_ok": None, "polls": 0}

    def poll_once(self) -> None:
        try:
            with self._opener.open(self.url, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))
            if isinstance(payload, dict) and payload.get("error"):
                raise ValueError(str(payload["error"]))
            data = summarize(payload)
            with self._lock:
                self._status.update(connected=True, error=None, data=data, last_ok=time.time())
        except (URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            with self._lock:
                self._status.update(connected=False, error=str(reason)[:160])
        finally:
            with self._lock:
                self._status["polls"] += 1

    def run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.period_s)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._status)
        st["age_s"] = None if st["last_ok"] is None else round(time.time() - st["last_ok"], 1)
        st["read_only"] = True
        return st
