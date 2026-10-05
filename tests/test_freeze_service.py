"""冻结/解冻领域服务回归测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from freeze_service import (  # noqa: E402
    ACCOUNTING,
    AUDITOR,
    ENTERPRISE,
    TRADING,
    AuthError,
    FreezeService,
    Principal,
    ServiceError,
    Store,
)

ENT = Principal("ent-1", ENTERPRISE)
ACC = Principal("acc-1", ACCOUNTING)
AUD = Principal("aud-1", AUDITOR)
TRD = Principal("trd-1", TRADING)


def utc(offset_hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = FreezeService(Store())
        self.svc.create_account("E", 1000)

    def activate(self, payload: dict) -> dict:
        case = self.svc.register_case(ENT, {"enterprise_id": "E", **payload})
        cid = case["case_id"]
        self.svc.submit_case(ENT, cid)
        self.svc.approve_case_step(ACC, cid)
        case = self.svc.approve_case_step(AUD, cid)
        self.assertEqual(case["status"], "active")
        return case

    # --------------------------------------------------------- 登记与审批

    def test_register_validates_amounts_and_chain(self) -> None:
        with self.assertRaises(ServiceError):
            self.svc.register_case(ENT, {"enterprise_id": "E", "freezes": []})
        with self.assertRaises(ServiceError):
            self.svc.register_case(
                ENT, {"enterprise_id": "E", "freezes": [{"amount": 0}]})
        with self.assertRaises(ServiceError):
            self.svc.register_case(
                ENT, {"enterprise_id": "E", "freezes": [{"amount": 100}],
                      "freeze_chain": ["不存在的角色"]})

    def test_approval_chain_must_follow_order_and_roles(self) -> None:
        case = self.svc.register_case(
            ENT, {"enterprise_id": "E", "freezes": [{"amount": 100}]})
        cid = case["case_id"]
        self.svc.submit_case(ENT, cid)
        # 监管审计员不能越过核算专员先批
        with self.assertRaises(AuthError):
            self.svc.approve_case_step(AUD, cid)
        # 交易运营员无权审批
        with self.assertRaises(AuthError):
            self.svc.approve_case_step(TRD, cid)
        self.svc.approve_case_step(ACC, cid)
        case = self.svc.get_case(AUD, cid)
        self.assertEqual(case["current_step"], 1)
        self.svc.approve_case_step(AUD, cid)
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "active")

    def test_reject_seals_case_and_does_not_freeze(self) -> None:
        case = self.svc.register_case(
            ENT, {"enterprise_id": "E", "freezes": [{"amount": 900}]})
        cid = case["case_id"]
        self.svc.submit_case(ENT, cid)
        self.svc.reject_case(ACC, cid, "证据不足")
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "rejected")
        self.assertEqual(self.svc.available_balance("E")["available"], 1000)

    # --------------------------------------------------------- 余额计算

    def test_general_freeze_deducted_dynamically(self) -> None:
        self.activate({"freezes": [{"amount": 300}]})
        avail = self.svc.available_balance("E")
        self.assertEqual((avail["balance"], avail["frozen_total"], avail["available"]),
                         (1000, 300, 700))

    def test_batch_freeze_does_not_block_other_business(self) -> None:
        # 1000 历史余额 + 批次 B1 600；只冻结 B1 的 200，其余 1400 照常可用
        self.svc.deposit("E", 600, "B1")
        self.activate({"freezes": [{"amount": 200, "source_batch": "B1"}]})
        avail = self.svc.available_balance("E")
        self.assertEqual(avail["frozen_total"], 200)
        self.assertEqual(avail["available"], 1400)
        self.assertEqual(avail["frozen_by_batch"], {"B1": 200})

    def test_batch_freeze_cannot_spend_frozen_portion(self) -> None:
        self.svc.deposit("E", 600, "B1")  # 总额 1600，B1 池 600
        self.activate({"freezes": [{"amount": 500, "source_batch": "B1"}]})
        # 可用 1100：B1 可花 100，历史余额 1000
        result = self.svc.execute_transaction(TRD, "E", 1100, "T1")
        self.assertEqual(result["balance_after"], 500)
        # 交易后 B1 仅剩冻结的 500，再花 1 元都不行
        with self.assertRaises(ServiceError):
            self.svc.execute_transaction(TRD, "E", 1, "T2")

    def test_two_cases_same_batch_hold_additively(self) -> None:
        self.svc.deposit("E", 600, "B1")  # 总额 1600
        self.activate({"freezes": [{"amount": 200, "source_batch": "B1"}]})
        self.activate({"freezes": [{"amount": 300, "source_batch": "B1"}]})
        avail = self.svc.available_balance("E")
        # 两案独立法律冻结：200+300=500，封顶于批次剩余 600
        self.assertEqual(avail["frozen_by_batch"], {"B1": 500})
        self.assertEqual(avail["available"], 1100)

    def test_pending_freeze_does_not_affect_balance(self) -> None:
        case = self.svc.register_case(ENT, {"enterprise_id": "E",
                                            "freezes": [{"amount": 900}]})
        self.svc.submit_case(ENT, case["case_id"])
        self.assertEqual(self.svc.available_balance("E")["available"], 1000)

    def test_future_effective_and_expiry_window(self) -> None:
        case = self.svc.register_case(ENT, {
            "enterprise_id": "E",
            "freezes": [{"amount": 500}],
            "effective_from": utc(1),
            "effective_to": utc(3),
        })
        cid = case["case_id"]
        self.svc.submit_case(ENT, cid)
        self.svc.approve_case_step(ACC, cid)
        self.svc.approve_case_step(AUD, cid)
        # 审批通过但未到生效时间：已确认、未冻结
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "confirmed")
        self.assertEqual(self.svc.available_balance("E")["frozen_total"], 0)
        # 到期批处理：先激活
        t2 = datetime.now(timezone.utc) + timedelta(hours=2)
        self.svc.sweep_expired(t2)
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "active")
        self.assertEqual(self.svc.available_balance("E", t2)["frozen_total"], 500)
        # 到期后自动失效
        t4 = datetime.now(timezone.utc) + timedelta(hours=4)
        self.svc.sweep_expired(t4)
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "sealed")
        self.assertEqual(self.svc.available_balance("E", t4)["frozen_total"], 0)

    # ------------------------------------------------ 扩大/缩减/续期/解冻

    def test_expand_bounded_by_registered_cap(self) -> None:
        case = self.activate({"freezes": [{"amount": 300, "cap_amount": 500}]})
        cid = case["case_id"]
        with self.assertRaises(ServiceError):
            self.svc.request_change(ACC, cid, "amend", {"amounts": {"": 501}})
        change = self.svc.request_change(ACC, cid, "amend", {"amounts": {"": 500}})
        self.svc.approve_change(ACC, change["change_id"])
        self.svc.approve_change(AUD, change["change_id"])
        self.assertEqual(self.svc.available_balance("E")["available"], 500)
        types = [(e["entry_type"], e["amount_delta"], e["amount_after"])
                 for e in self.svc.case_entries(AUD, cid)]
        self.assertIn(("freeze_expand", 200, 500), types)

    def test_shrink_releases_funds(self) -> None:
        case = self.activate({"freezes": [{"amount": 400}]})
        change = self.svc.request_change(
            ACC, case["case_id"], "amend", {"amounts": {"": 100}})
        # 审批链未完成前余额不变
        self.svc.approve_change(ACC, change["change_id"])
        self.assertEqual(self.svc.available_balance("E")["available"], 600)
        self.svc.approve_change(AUD, change["change_id"])
        self.assertEqual(self.svc.available_balance("E")["available"], 900)
        shrink = [e for e in self.svc.case_entries(AUD, case["case_id"])
                  if e["entry_type"] == "freeze_shrink"]
        self.assertEqual((shrink[0]["amount_delta"], shrink[0]["amount_after"]), (-300, 100))

    def test_renew_keeps_freeze_after_original_expiry(self) -> None:
        case = self.activate({
            "freezes": [{"amount": 500}],
            "effective_to": utc(1),
        })
        cid = case["case_id"]
        change = self.svc.request_change(
            ACC, cid, "amend", {"amounts": {}, "effective_to": utc(48)})
        self.svc.approve_change(ACC, change["change_id"])
        self.svc.approve_change(AUD, change["change_id"])
        t2 = datetime.now(timezone.utc) + timedelta(hours=2)
        self.svc.sweep_expired(t2)
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "active")
        self.assertEqual(self.svc.available_balance("E", t2)["frozen_total"], 500)

    def test_full_and_partial_unfreeze(self) -> None:
        self.svc.deposit("E", 500, "B1")  # 1500
        case = self.activate({"freezes": [
            {"amount": 200, "source_batch": "B1"},
            {"amount": 300},
        ]})
        cid = case["case_id"]
        batch_fid = [f["freeze_id"] for f in case["freezes"]
                     if f["source_batch"] == "B1"][0]
        # 部分解冻：仅解除批次冻结，案件仍执行中
        change = self.svc.request_change(
            AUD, cid, "unfreeze", {"freeze_ids": [batch_fid]})
        self.svc.approve_change(ACC, change["change_id"])
        self.svc.approve_change(AUD, change["change_id"])
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "active")
        self.assertEqual(self.svc.available_balance("E")["frozen_total"], 300)
        # 整案解冻后封存
        change = self.svc.request_change(AUD, cid, "unfreeze", {})
        self.svc.approve_change(ACC, change["change_id"])
        self.svc.approve_change(AUD, change["change_id"])
        self.assertEqual(self.svc.get_case(AUD, cid)["status"], "sealed")
        self.assertEqual(self.svc.available_balance("E")["available"], 1500)

    def test_change_requires_approval_chain(self) -> None:
        case = self.activate({"freezes": [{"amount": 300}]})
        with self.assertRaises(AuthError):
            self.svc.request_change(TRD, case["case_id"], "unfreeze", {})

    # ----------------------------------------------------------- 版本链

    def test_versions_carry_snapshots_and_parent_chain(self) -> None:
        case = self.activate({"freezes": [{"amount": 300, "cap_amount": 400}]})
        cid = case["case_id"]
        change = self.svc.request_change(ACC, cid, "amend", {"amounts": {"": 400}})
        self.svc.approve_change(ACC, change["change_id"])
        self.svc.approve_change(AUD, change["change_id"])
        versions = self.svc.case_versions(AUD, cid)
        actions = [v["action"] for v in versions]
        self.assertEqual(
            actions,
            ["created", "submitted", "approved_step", "activated",
             "amend_requested", "amend_effective"],
        )
        # 版本号严格递增，早期版本保留当时 300 的快照
        self.assertEqual([v["version_no"] for v in versions], list(range(1, 7)))
        self.assertEqual(versions[3]["snapshot"]["freezes"][0]["amount"], 300)
        self.assertEqual(versions[-1]["snapshot"]["freezes"][0]["amount"], 400)

    # ----------------------------------------------------------- 交易

    def test_transaction_records_check_snapshot_and_is_idempotent(self) -> None:
        case = self.activate({"freezes": [{"amount": 300}]})
        self.svc.execute_transaction(TRD, "E", 700, "T1")
        with self.assertRaises(ServiceError):
            self.svc.execute_transaction(TRD, "E", 700, "T1")
        history = self.svc.transaction_history(TRD, "E")
        self.assertEqual(len(history), 1)
        snap = history[0]["check_snapshot"]
        self.assertEqual(history[0]["frozen_at_check"], 300)
        self.assertEqual(snap["freezes"][0]["case_id"], case["case_id"])
        # 再交易被拒，拒绝记录也保留当时检查结果
        with self.assertRaises(ServiceError):
            self.svc.execute_transaction(TRD, "E", 1, "T2")
        checks = self.svc.balance_checks(AUD, "E")
        self.assertEqual([c["decision"] for c in checks], ["passed", "rejected"])
        rejected = checks[1]
        self.assertEqual((rejected["balance_before"], rejected["frozen_total"],
                          rejected["available"]), (300, 300, 0))

    def test_transaction_requires_trading_role(self) -> None:
        with self.assertRaises(AuthError):
            self.svc.execute_transaction(ENT, "E", 10, "T1")

    # ------------------------------------------------------- 证据隔离

    def test_evidence_only_visible_to_authorized_roles(self) -> None:
        case = self.svc.register_case(ENT, {
            "enterprise_id": "E",
            "freezes": [{"amount": 100}],
            "evidence_refs": ["ev://case-file/1", "ev://case-file/2"],
        })
        cid = case["case_id"]
        for principal in (ENT, TRD):
            view = self.svc.get_case(principal, cid)
            self.assertIsNone(view["evidence"])
            self.assertFalse(view["evidence_visible"])
            self.assertEqual(view["evidence_count"], 2)  # 只见数量不见内容
        for principal in (ACC, AUD):
            view = self.svc.get_case(principal, cid)
            self.assertEqual(view["evidence"],
                             ["ev://case-file/1", "ev://case-file/2"])


if __name__ == "__main__":
    unittest.main()
