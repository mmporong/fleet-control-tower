"""HTTP API checks against a live server on an ephemeral port: accounts, roles, CSRF, WMS keys, audit chain."""
from __future__ import annotations

from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from fleet.auth import Auth
from fleet.server import Runner, make_handler
from fleet.store import Store

PASSWORDS = {"김관제": "operator-pass-1", "viewer1": "viewer-pass-1", "boss": "supervisor-pass-1"}


class Client:
    """A browser: keeps the session cookie and sends the CSRF token it was given."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def call(self, method: str, path: str, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf, **(headers or {})}
        req = Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def login(self, name: str, password: str | None = None):
        status, res = self.call("POST", "/api/login", {"name": name, "password": password or PASSWORDS[name]})
        if status == 200:
            self.csrf = res["csrf"]
        return status, res


def serve(runner: Runner, auth: Auth | None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runner, None, auth))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.auth = Auth(cls.store)
        cls.auth.create_user("김관제", PASSWORDS["김관제"], "operator")
        cls.auth.create_user("viewer1", PASSWORDS["viewer1"], "viewer")
        cls.auth.create_user("boss", PASSWORDS["boss"], "supervisor")
        cls.wms_key = cls.auth.create_api_key("wms-test")
        cls.runner = Runner("grid-night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=False,
                            store=cls.store)
        cls.runner.start()
        cls.httpd, cls.base = serve(cls.runner, cls.auth)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.runner.stop()

    def operator(self) -> Client:
        c = Client(self.base)
        self.assertEqual(c.login("김관제")[0], 200)
        return c

    def test_everything_but_health_needs_a_session(self):
        anon = Client(self.base)
        self.assertEqual(anon.call("GET", "/health")[0], 200)
        for path in ("/api/snapshot", "/api/layout", "/api/audit"):
            self.assertEqual(anon.call("GET", path)[0], 401, path)
        self.assertEqual(anon.call("POST", "/api/command", {"cmd": "fleet_hold", "confirm": True})[0], 401)
        self.assertEqual(self.runner.sim.manager.mode, "RUNNING")

    def test_snapshot_and_layout(self):
        c = self.operator()
        status, snap = c.call("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(len(snap["robots"]), 20)
        self.assertIn("run", snap)
        self.assertGreater(len(c.call("GET", "/api/layout")[1]["nodes"]), 100)

    def test_command_is_audited_with_the_account_not_a_claimed_name(self):
        c = self.operator()
        status, res = c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-02"},
                             headers={"X-Operator": "someone-else"})
        self.assertEqual(status, 200, res)
        audit = c.call("GET", "/api/audit")[1]
        self.assertEqual(audit[-1]["operator"], "김관제")
        status, _ = c.call("POST", "/api/command", {"cmd": "no_such_command"})
        self.assertEqual(status, 400)
        self.assertEqual(c.call("GET", "/api/audit")[1][-1]["ok"], False)

    def test_csrf_origin_and_content_type_are_checked(self):
        c = self.operator()
        token, c.csrf = c.csrf, ""
        self.assertEqual(c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-03"})[0], 403)
        c.csrf = token
        status, _ = c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-03"},
                           headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-03"},
                           headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        host = self.base.split("://", 1)[1]
        status, _ = c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-03"},
                           headers={"Origin": f"http://{host}"})
        self.assertEqual(status, 200)

    def test_roles(self):
        viewer = Client(self.base)
        self.assertEqual(viewer.login("viewer1")[0], 200)
        self.assertEqual(viewer.call("GET", "/api/snapshot")[0], 200)
        self.assertEqual(viewer.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-04"})[0], 403)
        op = self.operator()
        self.assertEqual(op.call("POST", "/api/control", {"action": "speed", "value": 2.0})[0], 403)
        self.assertEqual(op.call("POST", "/api/command", {"cmd": "drill_comms", "robot": "AMR-04"})[0], 403)
        boss = Client(self.base)
        self.assertEqual(boss.login("boss")[0], 200)
        self.assertEqual(boss.call("POST", "/api/control", {"action": "speed", "value": 1.0})[0], 200)

    def test_wrong_passwords_lock_the_account(self):
        self.auth.create_user("victim", "victim-pass-1", "viewer")
        c = Client(self.base)
        for _ in range(5):
            self.assertEqual(c.login("victim", "wrong-password")[0], 401)
        status, res = c.login("victim", "victim-pass-1")
        self.assertEqual(status, 429, res)
        self.assertEqual(c.login("nobody", "whatever-123")[0], 401)

    def test_logout_ends_the_session(self):
        c = self.operator()
        self.assertEqual(c.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(c.call("GET", "/api/snapshot")[0], 401)

    def test_wms_orders_need_an_api_key_and_are_idempotent(self):
        anon = Client(self.base)
        order = {"order_id": "HTTP-1", "kind": "PICK", "source": "C3"}
        self.assertEqual(anon.call("POST", "/api/wms/orders", order)[0], 401)
        key = {"Authorization": f"Bearer {self.wms_key}"}
        s1, r1 = anon.call("POST", "/api/wms/orders", order, headers=key)
        s2, r2 = anon.call("POST", "/api/wms/orders", order, headers=key)
        s3, _ = anon.call("POST", "/api/wms/orders", {**order, "source": "C4"}, headers=key)
        self.assertEqual((s1, s2, s3), (201, 200, 409))
        self.assertEqual(r1["task"], r2["task"])
        self.assertIsNotNone(self.store.idem_get("HTTP-1"))          # survives a restart
        status, events = anon.call("GET", "/api/wms/events?after=0", headers=key)
        self.assertEqual(status, 200)
        mine = [e for e in events if e["task"] == r1["task"]]
        self.assertEqual(mine[0]["type"], "CREATED")
        self.assertEqual(mine[0]["ref"], "HTTP-1")
        last = events[-1]["seq"]
        self.assertTrue(all(e["seq"] > last for e in anon.call("GET", f"/api/wms/events?after={last}",
                                                                headers=key)[1]))

    def test_bad_json_is_rejected(self):
        c = self.operator()
        self.assertEqual(c.call("POST", "/api/command", b"{not json")[0], 400)

    def test_vda5050_inspector_and_local_view(self):
        c = self.operator()
        time.sleep(1.0)
        msgs = c.call("GET", "/api/vda5050/AMR-01?limit=5")[1]
        self.assertTrue(any(m["topic"].endswith("/state") for m in msgs))
        self.assertEqual(c.call("GET", "/api/local/AMR-01")[1]["robot"]["id"], "AMR-01")

    def test_audit_chain_detects_tampering(self):
        c = self.operator()
        c.call("POST", "/api/command", {"cmd": "pause_robot", "robot": "AMR-05"})
        self.assertEqual(c.call("GET", "/api/audit/verify")[1]["problems"], [])
        with self.store.lock:
            seq = self.store.db.execute("SELECT MIN(seq) FROM audit").fetchone()[0]
            self.store.db.execute("UPDATE audit SET body=replace(body, '김관제', 'mallory') WHERE seq=?", (seq,))
        problems = c.call("GET", "/api/audit/verify")[1]["problems"]
        self.assertTrue(problems)
        with self.store.lock:      # put it back for the other tests
            self.store.db.execute("UPDATE audit SET body=replace(body, 'mallory', '김관제') WHERE seq=?", (seq,))


class ReadOnlyAndDevModeTests(unittest.TestCase):
    def test_read_only_server_refuses_commands(self):
        store = Store(":memory:")
        auth = Auth(store)
        auth.create_user("op", "operator-pass-1", "operator")
        runner = Runner("grid-night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=True, store=store)
        httpd, base = serve(runner, auth)
        try:
            c = Client(base)
            c.login("op", "operator-pass-1")
            self.assertEqual(c.call("POST", "/api/command", {"cmd": "fleet_hold", "confirm": True})[0], 403)
            self.assertEqual(runner.sim.manager.mode, "RUNNING")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_no_auth_development_mode(self):
        runner = Runner("grid-night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=False)
        httpd, base = serve(runner, None)
        try:
            c = Client(base)
            self.assertEqual(c.call("GET", "/api/snapshot")[0], 200)
            me = c.call("GET", "/api/me")[1]
            self.assertFalse(me["auth"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
