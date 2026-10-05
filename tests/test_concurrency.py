"""冻结与交易并发一致性测试（文件库 + 多线程，真实锁竞争）。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from freeze_service import (  # noqa: E402
    ACCOUNTING,
    AUDITOR,
    ENTERPRISE,
    TRADING,
    FreezeService,
    Principal,
    ServiceError,
    Store,
)

ENT = Principal("ent-1", ENTERPRISE)
ACC = Principal("acc-1", ACCOUNTING)
AUD = Principal("aud-1", AUDITOR)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "freeze.sqlite3"
        self.service = FreezeService(Store(db))
        # 每个线程使用独立的服务实例（独立连接），共享同一个数据库
        self.db_path = db
        self.barrier = threading.Barrier(6)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self) -> FreezeService:
        return FreezeService(Store(self.db_path))

    def test_concurrent_transactions_never_overspend_frozen_balance(self) -> None:
        svc = self.service
        svc.create_account("E", 1000)
        case = svc.register_case(ENT, {"enterprise_id": "E", "freezes": [{"amount": 600}]})
        cid = case["case_id"]
        svc.submit_case(ENT, cid)
        svc.approve_case_step(ACC, cid)
        svc.approve_case_step(AUD, cid)
        self.assertEqual(svc.available_balance("E")["available"], 400)

        results: list[tuple[str, bool]] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            local = self._service()
            trd = Principal(f"trd-{worker_id}", TRADING)
            self.barrier.wait()
            for i in range(10):  # 6 线程 x 10 笔 x 10 元，申请额 600 > 可用 400
                ref = f"T-{worker_id}-{i}"
                try:
                    local.execute_transaction(trd, "E", 10, ref)
                    ok = True
                except ServiceError:
                    ok = False
                with lock:
                    results.append((ref, ok))

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        passed = sum(1 for _, ok in results if ok)
        # 冻结 600 后只有 400 可花：恰好 40 笔通过，其余拒绝；无超额扣款
        self.assertEqual(passed, 40)
        self.assertEqual(len(results), 60)
        account = svc.get_account("E")
        self.assertEqual(account["balance"], 600)
        self.assertEqual(svc.available_balance("E")["available"], 0)
        # 成交记录与扣款一一对应
        self.assertEqual(len(svc.transaction_history(AUD, "E")), 40)

    def test_freeze_activation_races_transactions(self) -> None:
        svc = self.service
        svc.create_account("R", 1000)
        case = svc.register_case(ENT, {"enterprise_id": "R", "freezes": [{"amount": 700}]})
        cid = case["case_id"]
        svc.submit_case(ENT, cid)
        svc.approve_case_step(ACC, cid)  # 留终审：由线程触发激活

        errors: list[BaseException] = []

        def activate() -> None:
            local = self._service()
            self.barrier.wait()
            try:
                local.approve_case_step(AUD, cid)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def transact(worker_id: int) -> None:
            local = self._service()
            trd = Principal(f"trd-{worker_id}", TRADING)
            self.barrier.wait()
            for i in range(20):
                try:
                    local.execute_transaction(trd, "R", 10, f"R-{worker_id}-{i}")
                except ServiceError:
                    pass
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=activate)]
        threads += [threading.Thread(target=transact, args=(w,)) for w in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # 不变量：案件激活；每笔通过的检查在当时都满足 available >= 金额；
        # 扣款总额与余额守恒；最终可用余额不为负。
        # （激活可能先于或晚于交易落锁：晚于激活最多花 300；早于激活的成交
        #   经当时检查合法，冻结只能覆盖其后剩余资金，不回溯已成交交易。）
        self.assertEqual(svc.get_case(AUD, cid)["status"], "active")
        balance = svc.get_account("R")["balance"]
        self.assertGreaterEqual(balance, 0)
        self.assertGreaterEqual(svc.available_balance("R")["available"], 0)
        for record in svc.balance_checks(AUD, "R"):
            if record["decision"] == "passed":
                self.assertGreaterEqual(record["available"], record["requested_amount"])
        # 扣款总额与余额守恒
        spent = sum(r["amount"] for r in svc.transaction_history(AUD, "R"))
        self.assertEqual(balance + spent, 1000)
        # 激活后冻结额封顶于剩余资金，可用余额恒不为负
        self.assertEqual(
            svc.available_balance("R")["frozen_total"], min(700, balance))

    def test_duplicate_txn_ref_rejected_under_concurrency(self) -> None:
        svc = self.service
        svc.create_account("D", 1000)
        outcomes: list[bool] = []
        lock = threading.Lock()
        bar = threading.Barrier(4)

        def fire() -> None:
            local = self._service()
            trd = Principal("trd-x", TRADING)
            bar.wait()
            try:
                local.execute_transaction(trd, "D", 100, "DUP")
                ok = True
            except ServiceError:
                ok = False
            with lock:
                outcomes.append(ok)

        threads = [threading.Thread(target=fire) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(svc.get_account("D")["balance"], 900)


if __name__ == "__main__":
    unittest.main()
