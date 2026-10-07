"""HTTP API 端到端测试（标准库 http.client）。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reg_freeze.api import create_server
from reg_freeze.repository import Repository
from reg_freeze.security import Principal
from reg_freeze.service import FreezeService

PRINCIPALS = {
    "t-filer": Principal("u-filer", "王申报", "企业申报员"),
    "t-accountant": Principal("u-acc", "李核算", "核算专员"),
    "t-operator": Principal("u-op", "赵运营", "交易运营员"),
    "t-auditor": Principal("u-aud", "孙审计", "监管审计员"),
}

TOKENS = {
    "t-filer": "Bearer t-filer",
    "t-accountant": "Bearer t-accountant",
    "t-operator": "Bearer t-operator",
    "t-auditor": "Bearer t-auditor",
}


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.svc = FreezeService(self.repo)
        self.httpd = create_server(
            self.svc, host="127.0.0.1", port=0, tokens=PRINCIPALS
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None
        )
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        parsed = json.loads(raw) if raw else {}
        return resp.status, parsed

    def ok(self, method: str, path: str, token: str, body: dict | None = None):
        status, parsed = self.call(method, path, token, body)
        self.assertEqual(status, 200, parsed)
        self.assertTrue(parsed["ok"], parsed)
        return parsed["data"]

    def seed_account(self) -> None:
        self.ok("POST", "/admin/accounts", TOKENS["t-operator"],
                {"account_id": "ACC", "balance": 10000})
        self.ok("POST", "/admin/sources", TOKENS["t-operator"],
                {"account_id": "ACC", "source_id": "SRC-A", "amount": 3000})


class AuthTest(ApiTestBase):
    def test_missing_token_403(self) -> None:
        status, parsed = self.call("GET", "/cases")
        self.assertEqual(status, 403)
        self.assertEqual(parsed["error"], "permission_denied")

    def test_bad_token_403(self) -> None:
        status, _ = self.call("GET", "/cases", "Bearer nope")
        self.assertEqual(status, 403)


class FreezeFlowApiTest(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_account()

    def test_register_submit_approve_flow(self) -> None:
        case = self.ok("POST", "/cases", TOKENS["t-filer"], {
            "account_id": "ACC", "amount_limit": 3000,
            "title": "稽查冻结", "evidence_ref": "doc-7",
            "evidence_text": "机密",
        })
        cid = case["case_id"]
        self.assertEqual(case["status"], "草稿")
        self.ok("POST", f"/cases/{cid}/submit", TOKENS["t-filer"], {"remark": "提交"})
        self.ok("POST", f"/cases/{cid}/approve", TOKENS["t-accountant"], {"remark": "同意"})
        detail = self.ok("GET", f"/cases/{cid}", TOKENS["t-auditor"])
        self.assertEqual(detail["status"], "执行中")

        balance = self.ok("GET", "/accounts/ACC/balance", TOKENS["t-operator"])
        self.assertEqual(balance["frozen"], 3000)
        self.assertEqual(balance["available"], 7000)

    def test_operator_role_cannot_register(self) -> None:
        status, parsed = self.call("POST", "/cases", TOKENS["t-operator"],
                                   {"account_id": "ACC", "amount_limit": 100})
        self.assertEqual(status, 403, parsed)

    def test_transaction_endpoint_and_history(self) -> None:
        case = self.ok("POST", "/cases", TOKENS["t-filer"],
                       {"account_id": "ACC", "amount_limit": 3000})
        cid = case["case_id"]
        self.ok("POST", f"/cases/{cid}/submit", TOKENS["t-filer"])
        self.ok("POST", f"/cases/{cid}/approve", TOKENS["t-accountant"])

        approved = self.ok("POST", "/accounts/ACC/transactions", TOKENS["t-operator"],
                           {"amount": 7000, "txn_ref": "API-1"})
        self.assertEqual(approved["decision"], "approved")
        status, rejected = self.call(
            "POST", "/accounts/ACC/transactions", TOKENS["t-operator"],
            {"amount": 1, "txn_ref": "API-2"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(rejected["data"]["decision"] == "approved")

        checks = self.ok("GET", "/accounts/ACC/checks", TOKENS["t-auditor"])
        self.assertEqual(len(checks), 2)
        self.assertTrue(all("snapshot" in c for c in checks))

    def test_evidence_isolation_over_http(self) -> None:
        case = self.ok("POST", "/cases", TOKENS["t-filer"], {
            "account_id": "ACC", "amount_limit": 100,
            "evidence_ref": "secret://1", "evidence_text": "密",
        })
        cid = case["case_id"]
        # 运营员看不到证据引用
        op_view = self.ok("GET", f"/cases/{cid}", TOKENS["t-operator"])
        self.assertIsNone(op_view["evidence_ref"])
        status, _ = self.call("GET", f"/cases/{cid}/evidence", TOKENS["t-operator"])
        self.assertEqual(status, 404)
        status, _ = self.call("GET", f"/cases/{cid}/approval-chain", TOKENS["t-operator"])
        self.assertEqual(status, 403)
        # 审计员可见
        aud_view = self.ok("GET", f"/cases/{cid}/evidence", TOKENS["t-auditor"])
        self.assertEqual(aud_view["content"], "密")

    def test_amendment_flow(self) -> None:
        case = self.ok("POST", "/cases", TOKENS["t-filer"],
                       {"account_id": "ACC", "amount_limit": 2000})
        cid = case["case_id"]
        self.ok("POST", f"/cases/{cid}/submit", TOKENS["t-filer"])
        self.ok("POST", f"/cases/{cid}/approve", TOKENS["t-accountant"])

        proposal = self.ok("POST", f"/cases/{cid}/amendments", TOKENS["t-accountant"],
                           {"action": "扩大", "amount_delta": 500})
        pid = proposal["proposal_id"]
        decided = self.ok("POST", f"/amendments/{pid}/approve", TOKENS["t-accountant"])
        self.assertEqual(decided["status"], "approved")
        balance = self.ok("GET", "/accounts/ACC/balance", TOKENS["t-operator"])
        self.assertEqual(balance["frozen"], 2500)

        # 缩减
        proposal2 = self.ok("POST", f"/cases/{cid}/amendments", TOKENS["t-filer"],
                            {"action": "缩减", "amount_delta": -500})
        self.ok("POST", f"/amendments/{proposal2['proposal_id']}/approve",
                TOKENS["t-accountant"])
        balance2 = self.ok("GET", "/accounts/ACC/balance", TOKENS["t-operator"])
        self.assertEqual(balance2["frozen"], 2000)

    def test_version_and_entries_visible(self) -> None:
        case = self.ok("POST", "/cases", TOKENS["t-filer"],
                       {"account_id": "ACC", "amount_limit": 1000})
        cid = case["case_id"]
        self.ok("POST", f"/cases/{cid}/submit", TOKENS["t-filer"])
        self.ok("POST", f"/cases/{cid}/approve", TOKENS["t-accountant"])
        detail = self.ok("GET", f"/cases/{cid}", TOKENS["t-auditor"])
        self.assertEqual(len(detail["versions"]), 1)
        self.assertEqual(detail["entries"][0]["amount_delta"], 1000)
        self.assertTrue(detail["entries"][0]["approver"])

    def test_unknown_route_404(self) -> None:
        status, parsed = self.call("GET", "/nope", TOKENS["t-auditor"])
        self.assertEqual(status, 404, parsed)

    def test_bad_json_409(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/cases", body="{not json",
                     headers={"Authorization": TOKENS["t-filer"],
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 409)
        conn.close()


if __name__ == "__main__":
    unittest.main()
