"""HTTP API checks against a live server on an ephemeral port."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fleet.server import Runner, make_handler


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = Runner("night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=False)
        cls.runner.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.runner, None))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.runner.stop()

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def post(self, path, body, operator="관제사"):
        req = Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Operator": quote(operator)})
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_snapshot_and_layout(self):
        snap = self.get("/api/snapshot")
        self.assertEqual(len(snap["robots"]), 20)
        self.assertIn("run", snap)
        layout = self.get("/api/layout")
        self.assertGreater(len(layout["nodes"]), 100)

    def test_command_with_korean_operator_is_audited(self):
        status, res = self.post("/api/command", {"cmd": "pause_robot", "robot": "AMR-02"}, operator="김관제")
        self.assertEqual(status, 200, res)
        audit = self.get("/api/audit")
        self.assertEqual(audit[-1]["operator"], "김관제")
        status, res = self.post("/api/command", {"cmd": "no_such_command"})
        self.assertEqual(status, 400)
        self.assertEqual(self.get("/api/audit")[-1]["ok"], False)

    def test_wms_idempotency_over_http(self):
        order = {"order_id": "HTTP-1", "kind": "PICK", "source": "C3"}
        s1, r1 = self.post("/api/wms/orders", order)
        s2, r2 = self.post("/api/wms/orders", order)
        s3, _ = self.post("/api/wms/orders", {**order, "source": "C4"})
        self.assertEqual((s1, s2, s3), (201, 200, 409))
        self.assertEqual(r1["task"], r2["task"])

    def test_bad_json_is_rejected(self):
        req = Request(self.base + "/api/command", data=b"{not json", method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_vda5050_inspector_and_local_view(self):
        time.sleep(1.0)
        msgs = self.get("/api/vda5050/AMR-01?limit=5")
        self.assertTrue(any(m["topic"].endswith("/state") for m in msgs))
        local = self.get("/api/local/AMR-01")
        self.assertEqual(local["robot"]["id"], "AMR-01")


class ReadOnlyServerTests(unittest.TestCase):
    def test_read_only_server_refuses_commands(self):
        runner = Runner("night", 3, "locks+resolve", 1.0, auto_operator=False, read_only=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runner, None))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            req = Request(base + "/api/command", data=json.dumps({"cmd": "fleet_hold", "confirm": True}).encode(),
                          method="POST", headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 403)
            self.assertEqual(runner.sim.manager.mode, "RUNNING")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
