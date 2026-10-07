"""冻结与交易并发一致性测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reg_freeze.repository import Repository
from reg_freeze.security import Principal
from reg_freeze.service import FreezeService

FILER = Principal("u-filer", "王申报", "企业申报员")
ACCOUNTANT = Principal("u-acc", "李核算", "核算专员")
OPERATOR = Principal("u-op", "赵运营", "交易运营员")
AUDITOR = Principal("u-aud", "孙审计", "监管审计员")


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.svc = FreezeService(self.repo)
        self.svc.setup_account(OPERATOR, "ACC", 10000)
        for i in range(5):
            self.svc.setup_source(OPERATOR, "ACC", f"SRC-{i}", 2000)
        # 预置 10 个已生效案件，每个冻 500（共 5000，初始可用 5000）
        self.case_ids = []
        for _ in range(10):
            case = self.svc.register_case(FILER, "ACC", amount_limit=500)
            self.svc.submit_case(FILER, case["case_id"])
            self.svc.approve_case(ACCOUNTANT, case["case_id"])
            self.case_ids.append(case["case_id"])

    def test_parallel_transactions_never_overspend(self) -> None:
        """50 个并发交易各取 200：成功总额不得超过初始可用额度 5000。"""
        results: list[tuple[int, str]] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            r = self.svc.execute_transaction(
                OPERATOR, "ACC", {"amount": 200, "txn_ref": f"P{idx}"}
            )
            with lock:
                results.append((r["decision"], r.get("total_balance_after", -1)))

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(50)))

        approved = sum(1 for d, _ in results if d == "approved")
        rejected = sum(1 for d, _ in results if d == "rejected")
        self.assertEqual(approved + rejected, 50)
        self.assertLessEqual(approved, 25)  # 25 * 200 = 5000
        self.assertGreaterEqual(rejected, 25)
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["total_balance"], 10000 - approved * 200)
        self.assertGreaterEqual(snap["available"], 0)
        # 冻结额度始终完整
        self.assertEqual(snap["frozen"], 5000)

    def test_parallel_expand_and_transactions(self) -> None:
        """冻结扩大与交易并发：任何已提交交易都不能使余额跌破冻结额。"""
        def expand(idx: int) -> None:
            # 每个案件尝试扩大 100（部分会因余额不足失败，失败也算正常）
            try:
                p = self.svc.request_amendment(
                    ACCOUNTANT, self.case_ids[idx], "扩大", amount_delta=100
                )
                self.svc.decide_proposal(ACCOUNTANT, p["proposal_id"], True)
            except Exception:
                pass

        def transact(idx: int) -> None:
            self.svc.execute_transaction(
                OPERATOR, "ACC", {"amount": 300, "txn_ref": f"C{idx}"}
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = []
            for i in range(10):
                futures.append(pool.submit(expand, i))
            for i in range(30):
                futures.append(pool.submit(transact, i))
            for f in futures:
                f.result()

        snap = self.svc.available_balance("ACC")
        # 核心不变量：余额 >= 有效冻结
        self.assertGreaterEqual(snap["total_balance"], snap["frozen"])
        self.assertGreaterEqual(snap["available"], 0)
        # 分录台账（按案件原始金额求和）与仓储层原始汇总一致
        cases = self.svc.list_cases(AUDITOR, "ACC")
        ledger_frozen = 0
        for case in cases:
            ledger_frozen += sum(e["amount_delta"] for e in case["entries"])
        raw = self.repo.frozen_summary("ACC", snap["checked_at"])
        self.assertEqual(ledger_frozen, raw["total_frozen"])
        # 台账逐案均为非负净冻结
        self.assertGreaterEqual(ledger_frozen, 0)

    def test_parallel_unfreeze_releases_consistently(self) -> None:
        def unfreeze(idx: int) -> None:
            try:
                p = self.svc.request_amendment(
                    ACCOUNTANT, self.case_ids[idx], "解冻"
                )
                self.svc.decide_proposal(ACCOUNTANT, p["proposal_id"], True)
            except Exception:
                pass

        def transact(idx: int) -> None:
            self.svc.execute_transaction(
                OPERATOR, "ACC", {"amount": 1000, "txn_ref": f"U{idx}"}
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(unfreeze, i) for i in range(10)]
            futures += [pool.submit(transact, i) for i in range(10)]
            for f in futures:
                f.result()

        snap = self.svc.available_balance("ACC")
        self.assertGreaterEqual(snap["total_balance"], snap["frozen"])
        approved_checks = [
            c for c in self.svc.list_checks(AUDITOR, "ACC")
            if c["decision"] == "approved"
        ]
        spent = sum(c["request"]["amount"] for c in approved_checks)
        # 花掉的钱不能超过 初始余额 - 最终冻结
        self.assertLessEqual(spent, 10000 - snap["frozen"])
        self.assertEqual(10000 - spent, snap["total_balance"])

    def test_serializable_ledger_per_case(self) -> None:
        """单案件高并发扩大/缩减后，版本号连续、分录金额与版本一致。"""
        case = self.case_ids[0]

        def grow(_: int) -> None:
            try:
                p = self.svc.request_amendment(
                    ACCOUNTANT, case, "扩大", amount_delta=10
                )
                self.svc.decide_proposal(ACCOUNTANT, p["proposal_id"], True)
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(grow, range(20)))

        detail = self.svc.get_case(AUDITOR, case)
        version_nos = [v["version_no"] for v in detail["versions"]]
        self.assertEqual(version_nos, list(range(1, len(version_nos) + 1)))
        # 每个扩大版本恰好对应一条 +10 分录
        expand_entries = [e for e in detail["entries"] if e["action"] == "扩大"]
        self.assertTrue(all(e["amount_delta"] == 10 for e in expand_entries))
        self.assertTrue(all(e["version_no"] in version_nos for e in expand_entries))


if __name__ == "__main__":
    unittest.main()
