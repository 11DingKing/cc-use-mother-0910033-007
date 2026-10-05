"""HTTP JSON API 端到端测试（标准库 http.server，真实端口）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from freeze_service.api import make_server  # noqa: E402

BASE = "http://127.0.0.1:{port}"


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db = str(Path(self._tmp.name) / "api.sqlite3")
        self.httpd = make_server("127.0.0.1", 0, db)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None,
             role: str = "auditor", actor: str = "u1"):
        url = BASE.format(port=self.port) + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Actor-Id", actor)
        req.add_header("X-Role", role)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def activate_case(self, freezes, role_actor="ent"):
        _, case = self.call("POST", "/cases",
                            {"enterprise_id": "E", "freezes": freezes,
                             "evidence_refs": ["ev://secret/1"]},
                            role="enterprise", actor=role_actor)
        cid = case["case_id"]
        self.call("POST", f"/cases/{cid}/submit", {}, role="enterprise", actor=role_actor)
        self.call("POST", f"/cases/{cid}/approve", {}, role="accounting", actor="acc")
        _, final = self.call("POST", f"/cases/{cid}/approve", {}, role="auditor", actor="aud")
        return final

    def test_full_flow_over_http(self) -> None:
        status, _ = self.call("POST", "/accounts",
                              {"enterprise_id": "E", "balance": 400})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/accounts/E/deposits",
                              {"amount": 600, "batch_no": "B1"},
                              role="enterprise", actor="ent")
        self.assertEqual(status, 200)

        case = self.activate_case([{"amount": 200, "source_batch": "B1"}])
        self.assertEqual(case["status"], "active")

        status, avail = self.call("GET", "/accounts/E/available")
        self.assertEqual(status, 200)
        self.assertEqual((avail["balance"], avail["frozen_total"], avail["available"]),
                         (1000, 200, 800))

        status, txn = self.call("POST", "/accounts/E/transactions",
                                {"amount": 800, "txn_ref": "T1"},
                                role="trading", actor="trd")
        self.assertEqual(status, 200)
        self.assertEqual(txn["balance_after"], 200)
        self.assertEqual(txn["frozen_at_check"], 200)

        # 历史成交保留当时检查结果
        status, history = self.call("GET", "/accounts/E/transactions",
                                    role="trading", actor="trd")
        self.assertEqual(history[0]["txn_ref"], "T1")
        self.assertIn("freezes", history[0]["check_snapshot"])

    def test_role_enforcement(self) -> None:
        self.call("POST", "/accounts", {"enterprise_id": "E", "balance": 1000})
        # 无请求头 -> 403
        req = urllib.request.Request(BASE.format(port=self.port) + "/accounts/E")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        # 交易运营员不能登记案件
        status, body = self.call("POST", "/cases",
                                 {"enterprise_id": "E", "freezes": [{"amount": 10}]},
                                 role="trading", actor="trd")
        self.assertEqual(status, 403)
        self.assertIn("无权", body["error"])
        # 企业申报员不能执行交易
        status, _ = self.call("POST", "/accounts/E/transactions",
                              {"amount": 1, "txn_ref": "X"},
                              role="enterprise", actor="ent")
        self.assertEqual(status, 403)

    def test_evidence_isolation_over_http(self) -> None:
        self.call("POST", "/accounts", {"enterprise_id": "E", "balance": 1000})
        case = self.activate_case([{"amount": 100}])
        cid = case["case_id"]
        status, auditor = self.call("GET", f"/cases/{cid}", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual(auditor["evidence"], ["ev://secret/1"])
        status, accounting = self.call("GET", f"/cases/{cid}", role="accounting")
        self.assertEqual(accounting["evidence"], ["ev://secret/1"])
        # 企业申报员、交易运营员只见数量
        status, enterprise = self.call("GET", f"/cases/{cid}", role="enterprise")
        self.assertIsNone(enterprise["evidence"])
        self.assertEqual(enterprise["evidence_count"], 1)
        status, trading = self.call("GET", f"/cases/{cid}", role="trading")
        self.assertIsNone(trading["evidence"])

    def test_change_flow_and_versions_over_http(self) -> None:
        self.call("POST", "/accounts", {"enterprise_id": "E", "balance": 1000})
        case = self.activate_case([{"amount": 300, "cap_amount": 500}])
        cid = case["case_id"]

        status, change = self.call("POST", f"/cases/{cid}/changes",
                                   {"kind": "amend",
                                    "payload": {"amounts": {"": 500}}},
                                   role="accounting", actor="acc")
        self.assertEqual(status, 200)
        chg = change["change_id"]
        self.call("POST", f"/changes/{chg}/approve", {}, role="accounting", actor="acc")
        status, final = self.call("POST", f"/changes/{chg}/approve", {},
                                  role="auditor", actor="aud")
        self.assertEqual(status, 200)
        self.assertEqual(final["status"], "effective")

        _, avail = self.call("GET", "/accounts/E/available")
        self.assertEqual(avail["available"], 500)

        status, versions = self.call("GET", f"/cases/{cid}/versions")
        self.assertEqual(status, 200)
        self.assertEqual(versions[-1]["action"], "amend_effective")

        status, entries = self.call("GET", f"/cases/{cid}/entries")
        self.assertIn("freeze_expand", [e["entry_type"] for e in entries])

    def test_unknown_route_404(self) -> None:
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
