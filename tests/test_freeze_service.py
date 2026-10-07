"""冻结服务领域行为测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reg_freeze.models import Action, CaseStatus, Scope, ScopeSpec
from reg_freeze.repository import Repository
from reg_freeze.security import (
    Conflict,
    NotFound,
    PermissionDenied,
    Principal,
)
from reg_freeze.service import FreezeService

FILER = Principal("u-filer", "王申报", "企业申报员")
FILER_OTHER = Principal("u-filer2", "其他企业", "企业申报员")
ACCOUNTANT = Principal("u-acc", "李核算", "核算专员")
OPERATOR = Principal("u-op", "赵运营", "交易运营员")
AUDITOR = Principal("u-aud", "孙审计", "监管审计员")


def iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.svc = FreezeService(self.repo)
        self.svc.setup_account(OPERATOR, "ACC", 10000)
        self.svc.setup_source(OPERATOR, "ACC", "SRC-A", 3000)
        self.svc.setup_source(OPERATOR, "ACC", "SRC-B", 2000)

    def register_approve(self, principal_approver=ACCOUNTANT, **kwargs):
        case = self.svc.register_case(FILER, "ACC", **kwargs)
        self.svc.submit_case(FILER, case["case_id"])
        self.svc.approve_case(principal_approver, case["case_id"])
        return self.svc.get_case(AUDITOR, case["case_id"])


class RegistrationTest(ServiceTestBase):
    def test_requires_positive_account_limit(self) -> None:
        with self.assertRaises(Conflict):
            self.svc.register_case(FILER, "ACC", amount_limit=0)

    def test_account_and_source_are_mutually_exclusive(self) -> None:
        with self.assertRaises(Conflict):
            self.svc.register_case(
                FILER, "ACC", amount_limit=100, sources=["SRC-A"]
            )

    def test_unknown_source_rejected(self) -> None:
        with self.assertRaises(Conflict):
            self.svc.register_case(FILER, "ACC", sources=["SRC-X"])

    def test_only_filer_registers(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.register_case(OPERATOR, "ACC", amount_limit=100)

    def test_expire_at_must_be_future(self) -> None:
        with self.assertRaises(Conflict):
            self.svc.register_case(
                FILER, "ACC", amount_limit=100, expire_at=iso_after(-10)
            )

    def test_unknown_account_404(self) -> None:
        with self.assertRaises(NotFound):
            self.svc.register_case(FILER, "ACC-NOPE", amount_limit=100)

    def test_approval_chain_must_be_approver_roles(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.register_case(
                FILER, "ACC", amount_limit=100, approval_chain=["交易运营员"]
            )


class LifecycleTest(ServiceTestBase):
    def test_full_lifecycle_statuses(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=100)
        self.assertEqual(case["status"], CaseStatus.DRAFT.value)
        self.svc.submit_case(FILER, case["case_id"])
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.PENDING.value,
        )
        self.svc.approve_case(ACCOUNTANT, case["case_id"])
        active = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertEqual(active["status"], CaseStatus.ACTIVE.value)
        self.assertEqual(active["current_version"], 1)

    def test_cannot_submit_twice(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=100)
        self.svc.submit_case(FILER, case["case_id"])
        with self.assertRaises(Conflict):
            self.svc.submit_case(FILER, case["case_id"])

    def test_operator_cannot_approve(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=100)
        self.svc.submit_case(FILER, case["case_id"])
        with self.assertRaises(PermissionDenied):
            self.svc.approve_case(OPERATOR, case["case_id"])

    def test_reject_returns_to_draft(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=100)
        self.svc.submit_case(FILER, case["case_id"])
        self.svc.reject_case(ACCOUNTANT, case["case_id"], "材料不全")
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.DRAFT.value,
        )

    def test_other_filer_cannot_submit(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=100)
        with self.assertRaises(PermissionDenied):
            self.svc.submit_case(FILER_OTHER, case["case_id"])

    def test_multi_level_chain_enforces_order(self) -> None:
        case = self.svc.register_case(
            FILER, "ACC", amount_limit=100,
            approval_chain=["核算专员", "监管审计员"],
        )
        self.svc.submit_case(FILER, case["case_id"])
        # 越级
        with self.assertRaises(PermissionDenied):
            self.svc.approve_case(AUDITOR, case["case_id"])
        self.svc.approve_case(ACCOUNTANT, case["case_id"])
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.PENDING.value,
        )
        # 不能重复在同一环节
        with self.assertRaises(PermissionDenied):
            self.svc.approve_case(ACCOUNTANT, case["case_id"])
        self.svc.approve_case(AUDITOR, case["case_id"])
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.ACTIVE.value,
        )


class BalanceDeductionTest(ServiceTestBase):
    def test_account_freeze_deducted_dynamically(self) -> None:
        self.register_approve(amount_limit=2000)
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["total_balance"], 10000)
        self.assertEqual(snap["frozen"], 2000)
        self.assertEqual(snap["available"], 8000)

    def test_source_freeze_deducted_by_batch(self) -> None:
        self.register_approve(sources={"SRC-A": 1000})
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["frozen"], 1000)
        self.assertEqual(snap["source_frozen"], {"SRC-A": 1000})
        self.assertEqual(snap["available"], 9000)

    def test_source_full_freeze_uses_current_batch_amount(self) -> None:
        self.register_approve(sources=["SRC-B"])
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["source_frozen"], {"SRC-B": 2000})

    def test_source_partial_limit_capped(self) -> None:
        self.register_approve(sources={"SRC-A": 99999})
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["source_frozen"], {"SRC-A": 3000})

    def test_multiple_cases_aggregate(self) -> None:
        self.register_approve(amount_limit=2000)
        self.register_approve(sources={"SRC-A": 1000})
        snap = self.svc.available_balance("ACC")
        self.assertEqual(snap["frozen"], 3000)
        self.assertEqual(snap["available"], 7000)
        self.assertEqual(len(snap["freeze_cases"]), 2)

    def test_draft_and_pending_do_not_freeze(self) -> None:
        case = self.svc.register_case(FILER, "ACC", amount_limit=2000)
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)
        self.svc.submit_case(FILER, case["case_id"])
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)

    def test_expired_freeze_not_deducted(self) -> None:
        self.register_approve(amount_limit=2000, expire_at=iso_after(60))
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 2000)
        self.svc.sweep(iso_after(120))
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)

    def test_deferred_effective_freezes_only_after_activation(self) -> None:
        case = self.svc.register_case(
            FILER, "ACC", amount_limit=700,
            effective_from=iso_after(300), expire_at=iso_after(3600),
        )
        self.svc.submit_case(FILER, case["case_id"])
        self.svc.approve_case(ACCOUNTANT, case["case_id"])
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.CONFIRMED.value,
        )
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)
        self.svc.sweep(iso_after(301))
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 700)
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.ACTIVE.value,
        )


class TransactionTest(ServiceTestBase):
    def test_uncontested_business_proceeds(self) -> None:
        self.register_approve(amount_limit=3000)
        ok = self.svc.execute_transaction(
            OPERATOR, "ACC", {"amount": 7000, "txn_ref": "T-OK"}
        )
        self.assertEqual(ok["decision"], "approved")
        blocked = self.svc.execute_transaction(
            OPERATOR, "ACC", {"amount": 1, "txn_ref": "T-BLOCK"}
        )
        self.assertEqual(blocked["decision"], "rejected")
        self.assertIn("可用余额", blocked["detail"])

    def test_source_locked_funds_block_batch_spend(self) -> None:
        self.register_approve(sources={"SRC-A": 1000})
        # 总额可用 9000，但 SRC-A 仅 2000 可用
        ok = self.svc.execute_transaction(
            OPERATOR, "ACC", {"amount": 2000, "sources": {"SRC-A": 2000}}
        )
        self.assertEqual(ok["decision"], "approved")
        blocked = self.svc.execute_transaction(
            OPERATOR, "ACC", {"amount": 1, "sources": {"SRC-A": 1}}
        )
        self.assertEqual(blocked["decision"], "rejected")
        self.assertIn("SRC-A", blocked["detail"])

    def test_rejected_transaction_does_not_move_balance(self) -> None:
        self.register_approve(amount_limit=8000)
        before = self.svc.available_balance("ACC")
        r = self.svc.execute_transaction(OPERATOR, "ACC", {"amount": 2001})
        self.assertEqual(r["decision"], "rejected")
        after = self.svc.available_balance("ACC")
        self.assertEqual(after["total_balance"], before["total_balance"])

    def test_only_operator_executes(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.execute_transaction(FILER, "ACC", {"amount": 1})

    def test_check_persists_snapshot(self) -> None:
        self.register_approve(amount_limit=3000)
        result = self.svc.check_balance(OPERATOR, "ACC", {"amount": 5000, "txn_ref": "CHK1"})
        stored = self.svc.get_check(AUDITOR, result["check_id"])
        self.assertEqual(stored["decision"], "approved")
        self.assertEqual(stored["snapshot"]["available"], 7000)
        self.assertEqual(stored["snapshot"]["frozen"], 3000)

    def test_invalid_requests(self) -> None:
        with self.assertRaises(Conflict):
            self.svc.check_balance(OPERATOR, "ACC", {"amount": 0})
        with self.assertRaises(Conflict):
            self.svc.check_balance(
                OPERATOR, "ACC",
                {"amount": 100, "sources": {"SRC-A": 40, "SRC-B": 70}},
            )


class AmendmentTest(ServiceTestBase):
    def _active_case(self, **kwargs):
        return self.register_approve(**kwargs)

    def test_expand_creates_version_and_entry(self) -> None:
        case = self._active_case(amount_limit=2000)
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.EXPAND, amount_delta=500
        )
        decided = self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        self.assertEqual(decided["status"], "approved")
        refreshed = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertEqual(refreshed["current_version"], 2)
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 2500)
        actions = [e["action"] for e in refreshed["entries"]]
        self.assertIn(Action.EXPAND.value, actions)
        versions = [v["action"] for v in refreshed["versions"]]
        self.assertEqual(versions[-1], Action.EXPAND.value)

    def test_reduce_creates_negative_entry(self) -> None:
        case = self._active_case(amount_limit=2000)
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.REDUCE, amount_delta=-500
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 1500)

    def test_cannot_reduce_below_frozen(self) -> None:
        case = self._active_case(amount_limit=2000)
        with self.assertRaises(Conflict):
            self.svc.request_amendment(
                ACCOUNTANT, case["case_id"], Action.REDUCE, amount_delta=-2001
            )

    def test_cannot_expand_beyond_balance(self) -> None:
        case = self._active_case(amount_limit=9000)
        with self.assertRaises(Conflict):
            self.svc.request_amendment(
                ACCOUNTANT, case["case_id"], Action.EXPAND, amount_delta=1001
            )

    def test_expand_source_batch(self) -> None:
        case = self._active_case(sources={"SRC-A": 1000})
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.EXPAND,
            source_deltas={"SRC-A": 500},
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        self.assertEqual(
            self.svc.available_balance("ACC")["source_frozen"], {"SRC-A": 1500}
        )

    def test_renew_extends_deadline(self) -> None:
        case = self._active_case(amount_limit=500, expire_at=iso_after(60))
        old_expire = self.svc.get_case(AUDITOR, case["case_id"])["expire_at"]
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.RENEW, extend_seconds=300
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        new_case = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertGreater(new_case["expire_at"], old_expire)
        # 原到期时刻 sweep 不封存
        self.svc.sweep(iso_after(120))
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.ACTIVE.value,
        )
        # 续期版本带零金额留痕分录
        self.assertEqual(new_case["entries"][-1]["amount_delta"], 0)

    def test_rejected_amendment_changes_nothing(self) -> None:
        case = self._active_case(amount_limit=2000)
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.EXPAND, amount_delta=500
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], False, "不准")
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 2000)
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["current_version"], 1
        )

    def test_unfreeze_requires_full_chain(self) -> None:
        case = self.svc.register_case(
            FILER, "ACC", amount_limit=300,
            approval_chain=["核算专员", "监管审计员"],
        )
        self.svc.submit_case(FILER, case["case_id"])
        self.svc.approve_case(ACCOUNTANT, case["case_id"])
        self.svc.approve_case(AUDITOR, case["case_id"])
        proposal = self.svc.request_amendment(
            FILER, case["case_id"], Action.UNFREEZE, remark="企业申诉成功"
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.ACTIVE.value,
        )
        self.svc.decide_proposal(AUDITOR, proposal["proposal_id"], True)
        sealed = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertEqual(sealed["status"], CaseStatus.SEALED.value)
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)

    def test_emergency_unfreeze_auditor_only(self) -> None:
        case = self._active_case(amount_limit=300)
        with self.assertRaises(PermissionDenied):
            self.svc.emergency_unfreeze(OPERATOR, case["case_id"])
        self.svc.emergency_unfreeze(AUDITOR, case["case_id"], "行政强制解除")
        self.assertEqual(
            self.svc.get_case(AUDITOR, case["case_id"])["status"],
            CaseStatus.SEALED.value,
        )
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)


class ExpiryTest(ServiceTestBase):
    def test_sweep_seals_and_releases(self) -> None:
        case = self.register_approve(amount_limit=500, expire_at=iso_after(60))
        result = self.svc.sweep(iso_after(61))
        self.assertIn(case["case_id"], result["expired"])
        sealed = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertEqual(sealed["status"], CaseStatus.SEALED.value)
        self.assertEqual(sealed["versions"][-1]["action"], Action.EXPIRE.value)
        # 到期写释放分录
        self.assertLess(sealed["entries"][-1]["amount_delta"], 0)
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 0)

    def test_sweep_idempotent(self) -> None:
        self.register_approve(amount_limit=500, expire_at=iso_after(60))
        first = self.svc.sweep(iso_after(61))
        second = self.svc.sweep(iso_after(62))
        self.assertEqual(len(first["expired"]), 1)
        self.assertEqual(second["expired"], [])


class HistoryTest(ServiceTestBase):
    def test_historical_check_keeps_snapshot_after_later_freeze(self) -> None:
        case = self.register_approve(amount_limit=2000)
        self.svc.execute_transaction(
            OPERATOR, "ACC", {"amount": 5000, "txn_ref": "H1"}
        )
        proposal = self.svc.request_amendment(
            ACCOUNTANT, case["case_id"], Action.EXPAND, amount_delta=500
        )
        self.svc.decide_proposal(ACCOUNTANT, proposal["proposal_id"], True)
        checks = self.svc.list_checks(AUDITOR)
        h1 = next(c for c in checks if c["txn_ref"] == "H1")
        self.assertEqual(h1["snapshot"]["frozen"], 2000)
        self.assertEqual(h1["snapshot"]["available"], 8000)
        self.assertEqual(h1["snapshot"]["total_balance"], 10000)
        # 当前视图反映新冻结
        self.assertEqual(self.svc.available_balance("ACC")["frozen"], 2500)


class EvidenceIsolationTest(ServiceTestBase):
    def _case_with_evidence(self):
        return self.svc.register_case(
            FILER, "ACC", amount_limit=100,
            title="稽查案件", evidence_ref="evidence://doc-007",
            evidence_text="内部稽查材料",
        )

    def test_auditor_sees_evidence(self) -> None:
        case = self._case_with_evidence()
        view = self.svc.get_case(AUDITOR, case["case_id"])
        self.assertEqual(view["evidence_ref"], "evidence://doc-007")
        ev = self.svc.get_evidence(AUDITOR, case["case_id"])
        self.assertEqual(ev["content"], "内部稽查材料")

    def test_accountant_sees_evidence(self) -> None:
        case = self._case_with_evidence()
        self.assertEqual(
            self.svc.get_case(ACCOUNTANT, case["case_id"])["evidence_ref"],
            "evidence://doc-007",
        )

    def test_owner_filer_sees_evidence(self) -> None:
        case = self._case_with_evidence()
        ev = self.svc.get_evidence(FILER, case["case_id"])
        self.assertEqual(ev["content"], "内部稽查材料")

    def test_operator_cannot_see_evidence(self) -> None:
        case = self._case_with_evidence()
        view = self.svc.get_case(OPERATOR, case["case_id"])
        self.assertIsNone(view["evidence_ref"])
        with self.assertRaises(NotFound):
            self.svc.get_evidence(OPERATOR, case["case_id"])
        with self.assertRaises(PermissionDenied):
            self.svc.get_approval_chain(OPERATOR, case["case_id"])

    def test_other_filer_cannot_see_case_or_evidence(self) -> None:
        case = self._case_with_evidence()
        with self.assertRaises(NotFound):
            self.svc.get_case(FILER_OTHER, case["case_id"])
        with self.assertRaises(NotFound):
            self.svc.get_evidence(FILER_OTHER, case["case_id"])


class ApprovalChainRecordTest(ServiceTestBase):
    def test_chain_records_every_actor(self) -> None:
        case = self.svc.register_case(
            FILER, "ACC", amount_limit=100, evidence_ref="doc",
            approval_chain=["核算专员", "监管审计员"],
        )
        self.svc.submit_case(FILER, case["case_id"], "提交")
        self.svc.approve_case(ACCOUNTANT, case["case_id"], "核算同意")
        self.svc.approve_case(AUDITOR, case["case_id"], "审计同意")
        chain = self.svc.get_approval_chain(AUDITOR, case["case_id"])
        self.assertEqual(chain["required_chain"], ["核算专员", "监管审计员"])
        actions = [(s["role"], s["action"]) for s in chain["steps"]]
        self.assertEqual(
            actions,
            [
                ("企业申报员", Action.REGISTER.value),
                ("企业申报员", Action.SUBMIT.value),
                ("核算专员", Action.APPROVE.value),
                ("监管审计员", Action.APPROVE.value),
            ],
        )
        # 能证明解冻/审批由谁批准：步骤里带姓名与时间
        self.assertTrue(all(s["name"] and s["decided_at"] for s in chain["steps"][1:]))


class ScopeSpecTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        scope = ScopeSpec(kind=Scope.SOURCE, sources=(("S1", 100), ("S2", None)))
        again = ScopeSpec.from_dict(scope.to_dict())
        self.assertEqual(again, scope)

    def test_invalid(self) -> None:
        with self.assertRaises(ValueError):
            ScopeSpec(kind=Scope.ACCOUNT, sources=(("S1", None),))
        with self.assertRaises(ValueError):
            ScopeSpec(kind=Scope.SOURCE)


if __name__ == "__main__":
    unittest.main()
