"""监管冻结解冻领域服务。

串起案件登记 → 审批链 → 生效冻结 → 扩大/缩减/续期/解冻 → 到期封存的完整流程，
并在同一并发模型下处理交易检查。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .models import (
    Action,
    ApprovalStep,
    Case,
    CaseStatus,
    CaseVersion,
    FreezeEntry,
    Scope,
    ScopeSpec,
)
from .repository import Repository
from .security import (
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_FILER,
    ROLE_OPERATOR,
    NotFound,
    PermissionDenied,
    Principal,
    Conflict,
    require_action,
)

APPROVER_ROLES = frozenset({ROLE_ACCOUNTANT, ROLE_AUDITOR})
DEFAULT_CHAIN = (ROLE_ACCOUNTANT,)
_PROPOSAL_ACTIONS = frozenset(
    {Action.EXPAND, Action.REDUCE, Action.RENEW, Action.UNFREEZE}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FreezeService:
    def __init__(
        self,
        repo: Repository,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repo = repo
        self._clock = clock

    # ---- 基础工具 -------------------------------------------------------

    def _now_iso(self) -> str:
        return self._clock().replace(microsecond=0).isoformat()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{self._clock().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    def _load_case_row(self, case_id: str):
        row = self.repo.get_case_row(case_id)
        if row is None:
            raise NotFound(f"案件不存在：{case_id}")
        return row

    # ---- 账户与来源批次 -------------------------------------------------

    def setup_account(self, principal: Principal, account_id: str, balance: int) -> dict:
        if principal.role not in (ROLE_OPERATOR, ROLE_ACCOUNTANT, ROLE_AUDITOR):
            raise PermissionDenied("无权维护账户")
        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                self.repo.upsert_account(account_id, balance)
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"account_id": account_id, "balance": balance}

    def setup_source(
        self, principal: Principal, account_id: str, source_id: str, amount: int
    ) -> dict:
        if principal.role not in (ROLE_OPERATOR, ROLE_ACCOUNTANT, ROLE_AUDITOR):
            raise PermissionDenied("无权维护来源批次")
        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                if self.repo.get_account(account_id) is None:
                    raise NotFound(f"账户不存在：{account_id}")
                self.repo.upsert_source(account_id, source_id, amount)
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"account_id": account_id, "source_id": source_id, "amount": amount}

    # ---- 案件登记 -------------------------------------------------------

    def register_case(
        self,
        principal: Principal,
        account_id: str,
        *,
        amount_limit: int | None = None,
        sources: Iterable[str] | dict[str, int | None] | None = None,
        expire_at: str | None = None,
        effective_from: str | None = None,
        title: str = "",
        evidence_ref: str = "",
        evidence_text: str = "",
        approval_chain: Iterable[str] = DEFAULT_CHAIN,
        case_id: str | None = None,
    ) -> dict:
        """登记冻结案件：额度范围（amount_limit）与来源条件（sources）。

        sources 可传批次号列表（冻结批次当前全额），或 ``{批次号: 上限}`` 映射，
        上限为 None 表示全额。额度范围与来源条件互斥（同一案件一种作用域）。
        """
        require_action(principal, Action.REGISTER)
        source_specs = self._normalize_sources(sources)
        scope = self._build_scope(amount_limit, source_specs)
        chain = tuple(approval_chain) or DEFAULT_CHAIN
        if not chain or any(role not in APPROVER_ROLES for role in chain):
            raise PermissionDenied("审批链必须由核算专员/监管审计员构成且非空")
        if expire_at is not None:
            expire_ts = self._parse_ts(expire_at, "expire_at")
        if effective_from is not None:
            self._parse_ts(effective_from, "effective_from")

        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                if self.repo.get_account(account_id) is None:
                    raise NotFound(f"账户不存在：{account_id}")
                if scope.kind is Scope.SOURCE:
                    for sid, _ in scope.sources:
                        if self.repo.get_source_amount(account_id, sid) <= 0:
                            raise Conflict(f"来源批次不存在或额度为零：{sid}")
                cid = case_id or self._new_id("FC")
                if self.repo.get_case_row(cid) is not None:
                    raise Conflict(f"案件编号已存在：{cid}")
                now = self._now_iso()
                if expire_at is not None and expire_ts <= self._clock():
                    raise Conflict("冻结期限必须晚于当前时间")
                case = Case(
                    case_id=cid,
                    account_id=account_id,
                    status=CaseStatus.DRAFT,
                    current_version=1,
                    created_by_role=principal.role,
                    created_by_name=principal.name,
                    created_at=now,
                    title=title,
                    evidence_ref=evidence_ref,
                )
                self.repo.insert_case(case)
                if evidence_text:
                    self.repo._conn.execute(
                        "INSERT INTO case_evidence(case_id, content, stored_at) VALUES(?,?,?)",
                        (cid, evidence_text, now),
                    )
                self.repo.insert_approval_step(
                    ApprovalStep(0, cid, principal.role, principal.name,
                                 Action.REGISTER, now, "登记冻结案件")
                )
                self.repo._conn.execute(
                    "INSERT INTO case_meta(case_id, approval_chain_json, effective_from) VALUES(?,?,?)",
                    (cid, json.dumps(list(chain), ensure_ascii=False), effective_from),
                )
                self.repo.insert_version(
                    CaseVersion(
                        version_no=1,
                        case_id=cid,
                        action=Action.REGISTER,
                        status_after=CaseStatus.DRAFT,
                        scope=scope,
                        effective_from=effective_from or now,
                        expire_at=expire_at,
                        approver_role=principal.role,
                        approver_name=principal.name,
                        created_at=now,
                        remark="登记（尚未生效）",
                    )
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_case(principal, cid)

    @staticmethod
    def _normalize_sources(
        sources: Iterable[str] | dict[str, int | None] | None,
    ) -> tuple[tuple[str, int | None], ...]:
        if sources is None:
            return ()
        if isinstance(sources, dict):
            items = tuple((str(k), v) for k, v in sources.items())
        else:
            items = tuple((str(s), None) for s in sources)
        if len(items) != len({sid for sid, _ in items}):
            raise Conflict("来源批次不能重复")
        for _, limit in items:
            if limit is not None and limit <= 0:
                raise Conflict("来源批次冻结上限必须为正或留空（全额）")
        return items

    @staticmethod
    def _build_scope(
        amount_limit: int | None,
        source_specs: tuple[tuple[str, int | None], ...],
    ) -> ScopeSpec:
        if source_specs:
            if amount_limit is not None:
                raise Conflict("额度范围与来源条件不能同时指定")
            return ScopeSpec(kind=Scope.SOURCE, sources=source_specs)
        if amount_limit is None or amount_limit <= 0:
            raise Conflict("账户级冻结必须指定正的额度范围")
        return ScopeSpec(kind=Scope.ACCOUNT, amount_limit=amount_limit)

    # ---- 提交与审批链 ---------------------------------------------------

    def submit_case(self, principal: Principal, case_id: str, remark: str = "") -> dict:
        require_action(principal, Action.SUBMIT)
        row = self._load_case_row(case_id)
        account_id = row["account_id"]
        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                row = self.repo.get_case_row(case_id)
                if row["status"] != CaseStatus.DRAFT.value:
                    raise Conflict("仅草稿状态案件可以提交核算")
                if row["created_by_name"] != principal.name and principal.role != ROLE_AUDITOR:
                    raise PermissionDenied("只能提交本企业登记的案件")
                now = self._now_iso()
                self.repo.insert_approval_step(
                    ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                 principal.name, Action.SUBMIT, now, remark)
                )
                self.repo.update_case_status(
                    case_id, CaseStatus.PENDING, int(row["current_version"])
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_case(principal, case_id)

    def _chain_for(self, case_id: str) -> list[str]:
        meta = self.repo._conn.execute(
            "SELECT approval_chain_json FROM case_meta WHERE case_id=?", (case_id,)
        ).fetchone()
        return json.loads(meta["approval_chain_json"]) if meta else list(DEFAULT_CHAIN)

    def _next_step_seq(self, case_id: str) -> int:
        row = self.repo._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
            "FROM approval_steps WHERE case_id=?",
            (case_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _chain_progress(self, case_id: str) -> int:
        """本案审批链已完成的审批人数（不含登记/提交动作）。"""
        row = self.repo._conn.execute(
            "SELECT COUNT(*) AS n FROM approval_steps WHERE case_id=? AND action=?",
            (case_id, Action.APPROVE.value),
        ).fetchone()
        return int(row["n"])

    def approve_case(self, principal: Principal, case_id: str, remark: str = "") -> dict:
        require_action(principal, Action.APPROVE)
        row = self._load_case_row(case_id)
        with self.repo.account_lock(row["account_id"]):
            self.repo.begin_write()
            try:
                row = self.repo.get_case_row(case_id)
                if row["status"] != CaseStatus.PENDING.value:
                    raise Conflict("仅待核算案件可以审批")
                chain = self._chain_for(case_id)
                progress = self._chain_progress(case_id)
                if progress >= len(chain) or principal.role != chain[progress]:
                    expected = chain[progress] if progress < len(chain) else "无"
                    raise PermissionDenied(
                        f"当前审批环节需要 {expected}，而非 {principal.role}"
                    )
                now_dt = self._clock()
                now = now_dt.replace(microsecond=0).isoformat()
                self.repo.insert_approval_step(
                    ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                 principal.name, Action.APPROVE, now,
                                 remark or "审批通过")
                )
                progress += 1
                if progress < len(chain):
                    # 审批链未走完，保持待核算，等待下一环节
                    self.repo.commit()
                    return self.get_case(principal, case_id)
                self._activate_case(case_id, now_dt)
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_case(principal, case_id)

    def reject_case(self, principal: Principal, case_id: str, remark: str = "") -> dict:
        require_action(principal, Action.REJECT)
        row = self._load_case_row(case_id)
        with self.repo.account_lock(row["account_id"]):
            self.repo.begin_write()
            try:
                row = self.repo.get_case_row(case_id)
                if row["status"] != CaseStatus.PENDING.value:
                    raise Conflict("仅待核算案件可以驳回")
                now = self._now_iso()
                self.repo.insert_approval_step(
                    ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                 principal.name, Action.REJECT, now, remark or "驳回")
                )
                self.repo.update_case_status(
                    case_id, CaseStatus.DRAFT, int(row["current_version"])
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_case(principal, case_id)

    def _activate_case(self, case_id: str, now_dt: datetime) -> None:
        """审批完成：未到生效时间进入已确认，否则进入执行中并写生效分录。"""
        row = self.repo.get_case_row(case_id)
        version = self.repo.latest_version(case_id)
        assert version is not None
        now = now_dt.replace(microsecond=0).isoformat()
        if version.effective_from > now:
            self.repo.update_case_status(
                case_id, CaseStatus.CONFIRMED, int(row["current_version"])
            )
            return
        self._write_activation_entries(case_id, version, now_dt, "审批通过生效")
        self.repo.update_case_status(
            case_id, CaseStatus.ACTIVE, int(row["current_version"])
        )

    def _write_activation_entries(
        self, case_id: str, version: CaseVersion, now_dt: datetime, note: str
    ) -> list[FreezeEntry]:
        row = self.repo.get_case_row(case_id)
        now = now_dt.replace(microsecond=0).isoformat()
        entries: list[FreezeEntry] = []
        scope = version.scope
        if scope.kind is Scope.ACCOUNT:
            entries.append(
                FreezeEntry(0, case_id, version.version_no, Action.APPROVE,
                            int(scope.amount_limit), None, ROLE_ACCOUNTANT, "",
                            version.approver_name, now, note)
            )
        else:
            for sid, limit in scope.sources:
                held = self.repo.get_source_amount(row["account_id"], sid)
                amount = held if limit is None else min(int(limit), held)
                if amount <= 0:
                    continue
                entries.append(
                    FreezeEntry(0, case_id, version.version_no, Action.APPROVE,
                                amount, sid, ROLE_ACCOUNTANT, "",
                                version.approver_name, now, note)
                )
        for entry in entries:
            entry.entry_id = self.repo.insert_entry(entry)
        return entries

    # ---- 变更申请（扩大/缩减/续期/解冻）审批链 --------------------------

    def request_amendment(
        self,
        principal: Principal,
        case_id: str,
        action: Action | str,
        *,
        amount_delta: int = 0,
        source_deltas: dict[str, int] | None = None,
        expire_at: str | None = None,
        extend_seconds: int | None = None,
        remark: str = "",
    ) -> dict:
        action = Action(action)
        if action not in _PROPOSAL_ACTIONS:
            raise Conflict("仅扩大/缩减/续期/解冻需要走变更申请")
        require_action(principal, action)
        row = self._load_case_row(case_id)
        params = self._validate_amendment_params(
            action, amount_delta, source_deltas, expire_at, extend_seconds
        )
        with self.repo.account_lock(row["account_id"]):
            self.repo.begin_write()
            try:
                row = self.repo.get_case_row(case_id)
                if row["status"] not in (CaseStatus.ACTIVE.value, CaseStatus.CONFIRMED.value):
                    raise Conflict("仅生效中的案件可以申请变更")
                self._validate_amendment_scope(case_id, action, params)
                pid = self._new_id("PR")
                self.repo._conn.execute(
                    "INSERT INTO case_proposals(proposal_id, case_id, action, payload_json, "
                    "requested_by_role, requested_by_name, status, requested_at, remark) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        pid, case_id, action.value,
                        json.dumps(params, ensure_ascii=False),
                        principal.role, principal.name, "pending",
                        self._now_iso(), remark,
                    ),
                )
                self.repo.insert_approval_step(
                    ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                 principal.name, action, self._now_iso(),
                                 f"申请{action.value}：{remark}")
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_proposal(principal, pid)

    def _validate_amendment_scope(
        self, case_id: str, action: Action, params: dict[str, Any]
    ) -> None:
        """扩大/缩减的作用域、上限与额度可用性校验（持锁事务内调用）。"""
        row = self.repo.get_case_row(case_id)
        version = self.repo.latest_version(case_id)
        now = self._now_iso()
        if action is Action.RENEW:
            new_expire = params.get("expire_at")
            if new_expire is not None:
                target = new_expire
            elif params.get("extend_seconds"):
                base = version.expire_at or now
                target = (
                    self._parse_ts(base)
                    + timedelta(seconds=int(params["extend_seconds"]))
                ).replace(microsecond=0).isoformat()
            else:
                return
            if target <= now:
                raise Conflict("续期后的到期时间必须晚于当前时间")
            return
        if action not in (Action.EXPAND, Action.REDUCE):
            return
        delta_acc = int(params.get("amount_delta") or 0)
        src_deltas: dict[str, int] = params.get("source_deltas") or {}
        if version.scope.kind is Scope.ACCOUNT and src_deltas:
            raise Conflict("账户级冻结不能追加来源批次条件")
        if version.scope.kind is Scope.SOURCE and delta_acc:
            raise Conflict("来源条件冻结不能按账户额度变更")
        summary = self.repo.frozen_summary(row["account_id"], self._now_iso())
        bucket = next(
            (b for b in summary["cases"] if b["case_id"] == case_id), None
        )
        case_frozen = bucket["account_amount"] if bucket else 0
        case_src = bucket["source_amounts"] if bucket else {}
        if action is Action.REDUCE:
            if delta_acc and -delta_acc > case_frozen:
                raise Conflict("缩减额度不能超过该案件当前账户级冻结额")
            for sid, d in src_deltas.items():
                if -d > case_src.get(sid, 0):
                    raise Conflict(f"缩减额度不能超过批次 {sid} 当前冻结额")
        else:  # EXPAND
            if delta_acc:
                balance = int(self.repo.get_account(row["account_id"])["balance"])
                if summary["account_frozen"] + delta_acc > balance - summary["source_frozen_total"]:
                    raise Conflict("扩大后账户级冻结总额不能超过账户可用余额")
            for sid, d in src_deltas.items():
                held = self.repo.get_source_amount(row["account_id"], sid)
                if held <= 0:
                    raise Conflict(f"来源批次不存在：{sid}")
                if d > held - case_src.get(sid, 0):
                    raise Conflict(f"批次 {sid} 可冻结额度不足")

    @staticmethod
    def _validate_amendment_params(
        action: Action,
        amount_delta: int,
        source_deltas: dict[str, int] | None,
        expire_at: str | None,
        extend_seconds: int | None,
    ) -> dict[str, Any]:
        source_deltas = source_deltas or {}
        if action is Action.EXPAND:
            if amount_delta < 0 or any(d < 0 for d in source_deltas.values()):
                raise Conflict("扩大的额度增量不能为负")
            if amount_delta == 0 and not source_deltas:
                raise Conflict("扩大必须给出正的额度增量")
        elif action is Action.REDUCE:
            if amount_delta > 0 or any(d > 0 for d in source_deltas.values()):
                raise Conflict("缩减的额度增量必须为负")
            if amount_delta == 0 and not source_deltas:
                raise Conflict("缩减必须给出负的额度增量")
        elif action is Action.RENEW:
            if expire_at is None and extend_seconds is None:
                raise Conflict("续期必须给出新的到期时间或顺延秒数")
            if extend_seconds is not None and extend_seconds <= 0:
                raise Conflict("顺延秒数必须为正")
            if expire_at is not None:
                FreezeService._parse_ts(expire_at, "expire_at")
        return {
            "amount_delta": amount_delta,
            "source_deltas": dict(source_deltas),
            "expire_at": expire_at,
            "extend_seconds": extend_seconds,
        }

    def list_proposals(self, principal: Principal, case_id: str | None = None) -> list[dict]:
        if principal.role not in (ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_FILER):
            raise PermissionDenied("无权查看变更申请")
        if case_id:
            self._load_case_row(case_id)
        if case_id:
            rows = self.repo._conn.execute(
                "SELECT * FROM case_proposals WHERE case_id=? ORDER BY requested_at",
                (case_id,),
            ).fetchall()
        else:
            rows = self.repo._conn.execute(
                "SELECT * FROM case_proposals ORDER BY requested_at"
            ).fetchall()
        return [self._proposal_dict(row) for row in rows]

    def get_proposal(self, principal: Principal, proposal_id: str) -> dict:
        if principal.role not in (ROLE_ACCOUNTANT, ROLE_AUDITOR, ROLE_FILER):
            raise PermissionDenied("无权查看变更申请")
        row = self.repo._conn.execute(
            "SELECT * FROM case_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"变更申请不存在：{proposal_id}")
        return self._proposal_dict(row)

    def decide_proposal(
        self, principal: Principal, proposal_id: str, approve: bool, remark: str = ""
    ) -> dict:
        prow = self.repo._conn.execute(
            "SELECT * FROM case_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if prow is None:
            raise NotFound(f"变更申请不存在：{proposal_id}")
        case_id = prow["case_id"]
        action = Action(prow["action"])
        require_action(principal, Action.APPROVE if approve else Action.REJECT)
        row = self._load_case_row(case_id)
        with self.repo.account_lock(row["account_id"]):
            self.repo.begin_write()
            try:
                prow = self.repo._conn.execute(
                    "SELECT * FROM case_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                if prow["status"] not in ("pending", "chaining"):
                    raise Conflict("该申请已有结论")
                now = self._now_iso()
                chain = self._chain_for(case_id)
                # 解冻沿用案件完整审批链；其余变更由核算专员单级审批
                required = chain if action is Action.UNFREEZE else [ROLE_ACCOUNTANT]
                steps = json.loads(prow["chain_steps_json"] or "[]")
                idx = len(steps)
                if idx >= len(required):
                    raise Conflict("审批链环节异常")
                if approve and principal.role != required[idx]:
                    raise PermissionDenied(
                        f"当前审批环节需要 {required[idx]}，而非 {principal.role}"
                    )
                if not approve:
                    self.repo._conn.execute(
                        "UPDATE case_proposals SET status='rejected', decided_at=? "
                        "WHERE proposal_id=?",
                        (now, proposal_id),
                    )
                    self.repo.insert_approval_step(
                        ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                     principal.name, Action.REJECT, now,
                                     f"驳回{action.value}申请：{remark}")
                    )
                    self.repo.commit()
                    return self.get_proposal(principal, proposal_id)
                steps.append(
                    {"role": principal.role, "name": principal.name, "at": now, "remark": remark}
                )
                if len(steps) < len(required):
                    self.repo._conn.execute(
                        "UPDATE case_proposals SET status='chaining', chain_steps_json=? "
                        "WHERE proposal_id=?",
                        (json.dumps(steps, ensure_ascii=False), proposal_id),
                    )
                    self.repo.insert_approval_step(
                        ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                     principal.name, Action.APPROVE, now,
                                     f"{action.value}申请审批环节通过（{len(steps)}/{len(required)}）")
                    )
                    self.repo.commit()
                    return self.get_proposal(principal, proposal_id)
                # 全部环节通过：落地变更
                params = json.loads(prow["payload_json"])
                self._apply_amendment(case_id, action, params, principal, now)
                self.repo._conn.execute(
                    "UPDATE case_proposals SET status='approved', chain_steps_json=?, "
                    "decided_at=? WHERE proposal_id=?",
                    (json.dumps(steps, ensure_ascii=False), now, proposal_id),
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_proposal(principal, proposal_id)

    def _apply_amendment(
        self,
        case_id: str,
        action: Action,
        params: dict[str, Any],
        principal: Principal,
        now: str,
    ) -> None:
        row = self.repo.get_case_row(case_id)
        account_id = row["account_id"]
        version = self.repo.latest_version(case_id)
        assert version is not None
        new_no = version.version_no + 1
        scope = ScopeSpec.from_dict(version.scope.to_dict())
        expire_at = version.expire_at
        new_entries: list[FreezeEntry] = []

        def add_entry(delta: int, source_id: str | None, note: str) -> None:
            new_entries.append(
                FreezeEntry(0, case_id, new_no, action, delta, source_id,
                            principal.role, principal.name, principal.name, now, note)
            )

        summary = self.repo.frozen_summary(account_id, now)
        bucket = next(
            (b for b in summary["cases"] if b["case_id"] == case_id),
            {"account_amount": 0, "source_amounts": {}},
        )

        if action in (Action.EXPAND, Action.REDUCE):
            sign = 1 if action is Action.EXPAND else -1
            delta_acc = int(params.get("amount_delta") or 0)
            src_deltas: dict[str, int] = params.get("source_deltas") or {}
            if scope.kind is Scope.ACCOUNT:
                if delta_acc:
                    add_entry(sign * abs(delta_acc), None, f"{action.value}账户额度")
                    scope = ScopeSpec(
                        kind=Scope.ACCOUNT,
                        amount_limit=max(0, int(scope.amount_limit) + sign * abs(delta_acc)),
                    )
            else:
                merged: dict[str, int | None] = {sid: lim for sid, lim in scope.sources}
                for sid, d in src_deltas.items():
                    if d == 0:
                        continue
                    add_entry(sign * abs(d), sid, f"{action.value}批次额度")
                    old_limit = merged.get(sid)
                    base = (
                        old_limit
                        if old_limit is not None
                        else bucket["source_amounts"].get(sid, 0)
                    )
                    merged[sid] = max(0, int(base) + sign * abs(d))
                scope = ScopeSpec(
                    kind=Scope.SOURCE,
                    sources=tuple((sid, merged[sid]) for sid in merged),
                )
        elif action is Action.RENEW:
            if params.get("expire_at"):
                expire_at = params["expire_at"]
            elif params.get("extend_seconds"):
                base = self._parse_ts(version.expire_at or now)
                expire_at = (
                    base + timedelta(seconds=int(params["extend_seconds"]))
                ).replace(microsecond=0).isoformat()
            # 续期不改变占用额度，仍生成零金额分录留痕
            add_entry(0, None, f"续期至 {expire_at}")
        elif action is Action.UNFREEZE:
            if bucket["account_amount"] > 0:
                add_entry(-bucket["account_amount"], None, "解冻释放账户额度")
            for sid, amount in sorted(bucket["source_amounts"].items()):
                if amount > 0:
                    add_entry(-amount, sid, "解冻释放批次额度")

        status_after = CaseStatus.SEALED if action is Action.UNFREEZE else CaseStatus.ACTIVE
        new_version = CaseVersion(
            version_no=new_no,
            case_id=case_id,
            action=action,
            status_after=status_after,
            scope=scope,
            effective_from=now,
            expire_at=expire_at,
            approver_role=principal.role,
            approver_name=principal.name,
            created_at=now,
            remark=action.value,
        )
        for entry in new_entries:
            entry.entry_id = self.repo.insert_entry(entry)
        self.repo.insert_version(new_version)
        self.repo.update_case_status(case_id, status_after, new_no)

    # ---- 监管审计员紧急解冻（单角色，仍全程留痕） ------------------------

    def emergency_unfreeze(
        self, principal: Principal, case_id: str, remark: str = "监管紧急解冻"
    ) -> dict:
        if principal.role != ROLE_AUDITOR:
            raise PermissionDenied("仅监管审计员可以紧急解冻")
        row = self._load_case_row(case_id)
        with self.repo.account_lock(row["account_id"]):
            self.repo.begin_write()
            try:
                row = self.repo.get_case_row(case_id)
                if row["status"] == CaseStatus.SEALED.value:
                    raise Conflict("案件已封存，无需解冻")
                now = self._now_iso()
                self.repo.insert_approval_step(
                    ApprovalStep(self._next_step_seq(case_id), case_id, principal.role,
                                 principal.name, Action.UNFREEZE, now, remark)
                )
                version = self.repo.latest_version(case_id)
                new_no = int(version.version_no) + 1
                summary = self.repo.frozen_summary(row["account_id"], now)
                bucket = next(
                    (b for b in summary["cases"] if b["case_id"] == case_id),
                    {"account_amount": 0, "source_amounts": {}},
                )
                entries: list[FreezeEntry] = []
                if bucket["account_amount"] > 0:
                    entries.append(
                        FreezeEntry(0, case_id, new_no, Action.UNFREEZE,
                                    -bucket["account_amount"], None, principal.role,
                                    principal.name, principal.name, now, remark)
                    )
                for sid, amount in sorted(bucket["source_amounts"].items()):
                    if amount > 0:
                        entries.append(
                            FreezeEntry(0, case_id, new_no, Action.UNFREEZE,
                                        -amount, sid, principal.role, principal.name,
                                        principal.name, now, remark)
                        )
                for entry in entries:
                    entry.entry_id = self.repo.insert_entry(entry)
                self.repo.insert_version(
                    CaseVersion(new_no, case_id, Action.UNFREEZE, CaseStatus.SEALED,
                                version.scope, now, version.expire_at,
                                principal.role, principal.name, now, remark)
                )
                self.repo.update_case_status(case_id, CaseStatus.SEALED, new_no)
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return self.get_case(principal, case_id)

    # ---- 到期/定时激活 ---------------------------------------------------

    def sweep(self, at: str | None = None) -> dict:
        """到点激活已确认案件；冻结期限届满自动封存（保留版本）。"""
        now_dt = self._clock()
        now = at or now_dt.replace(microsecond=0).isoformat()
        activated: list[str] = []
        expired: list[str] = []
        for case_id in self.repo.list_due_confirmations(now):
            row = self.repo.get_case_row(case_id)
            with self.repo.account_lock(row["account_id"]):
                self.repo.begin_write()
                try:
                    fresh = self.repo.get_case_row(case_id)
                    if fresh is None or fresh["status"] != CaseStatus.CONFIRMED.value:
                        self.repo.rollback()
                        continue
                    version = self.repo.latest_version(case_id)
                    self._write_activation_entries(case_id, version, now_dt, "到点自动生效")
                    self.repo.update_case_status(
                        case_id, CaseStatus.ACTIVE, int(fresh["current_version"])
                    )
                    self.repo.commit()
                    activated.append(case_id)
                except Exception:
                    self.repo.rollback()
                    raise
        for case_id in self.repo.list_expired_case_ids(now):
            row = self.repo.get_case_row(case_id)
            with self.repo.account_lock(row["account_id"]):
                self.repo.begin_write()
                try:
                    fresh = self.repo.get_case_row(case_id)
                    if fresh is None or fresh["status"] not in (
                        CaseStatus.ACTIVE.value, CaseStatus.CONFIRMED.value
                    ):
                        self.repo.rollback()
                        continue
                    version = self.repo.latest_version(case_id)
                    new_no = int(version.version_no) + 1
                    bucket = self.repo.case_net_frozen(case_id)
                    if bucket["account_amount"] > 0:
                        self.repo.insert_entry(
                            FreezeEntry(0, case_id, new_no, Action.EXPIRE,
                                        -bucket["account_amount"], None,
                                        "系统", "到期自动封存", "系统", now,
                                        "冻结期限届满，释放账户额度")
                        )
                    for sid, amount in sorted(bucket["source_amounts"].items()):
                        if amount > 0:
                            self.repo.insert_entry(
                                FreezeEntry(0, case_id, new_no, Action.EXPIRE,
                                            -amount, sid, "系统", "到期自动封存",
                                            "系统", now, "冻结期限届满，释放批次额度")
                            )
                    self.repo.insert_version(
                        CaseVersion(new_no, case_id, Action.EXPIRE, CaseStatus.SEALED,
                                    version.scope, now, version.expire_at,
                                    "系统", "到期自动封存", now, "冻结期限届满")
                    )
                    self.repo.update_case_status(case_id, CaseStatus.SEALED, new_no)
                    self.repo.commit()
                    expired.append(case_id)
                except Exception:
                    self.repo.rollback()
                    raise
        return {"activated": activated, "expired": expired, "at": now}

    # ---- 可用余额：动态扣除有效冻结 --------------------------------------

    def available_balance(self, account_id: str, now: str | None = None) -> dict:
        now = now or self._now_iso()
        row = self.repo.get_account(account_id)
        if row is None:
            raise NotFound(f"账户不存在：{account_id}")
        return self._balance_snapshot(account_id, int(row["balance"]), now)

    def _balance_snapshot(self, account_id: str, balance: int, now: str) -> dict:
        summary = self.repo.frozen_summary(account_id, now)
        # 来源批次冻结优先占用对应批次额度，账户级冻结占用剩余部分
        source_reserves: dict[str, int] = {}
        for sid, frozen in summary["source_frozen"].items():
            held = self.repo.get_source_amount(account_id, sid)
            source_reserves[sid] = min(frozen, held)
        source_total = sum(source_reserves.values())
        account_reserve = min(summary["account_frozen"], max(0, balance - source_total))
        frozen_total = account_reserve + source_total
        return {
            "account_id": account_id,
            "checked_at": now,
            "total_balance": balance,
            "account_frozen": account_reserve,
            "source_frozen": source_reserves,
            "frozen": frozen_total,
            "available": balance - frozen_total,
            "freeze_cases": summary["cases"],
        }

    def check_balance(
        self,
        principal: Principal,
        account_id: str,
        request: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict:
        """登记一笔余额检查（不改动余额），历史保留当时快照。"""
        amount, sources = self._normalize_request(request)
        now = self._now_iso()
        check_id = self._new_id("CHK")
        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                snap = self._evaluate(account_id, amount, sources, now)
                if persist:
                    self.repo.insert_check({
                        "check_id": check_id,
                        "account_id": account_id,
                        "txn_ref": str(request.get("txn_ref", "")),
                        "checked_at": now,
                        "total_balance": snap["total_balance"],
                        "frozen": snap["frozen"],
                        "available": snap["available"],
                        "request": {"amount": amount, "sources": sources},
                        "decision": snap["decision"],
                        "detail": snap["detail"],
                        "snapshot": snap,
                    })
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"check_id": check_id, **snap}

    def execute_transaction(
        self, principal: Principal, account_id: str, request: dict[str, Any]
    ) -> dict:
        """交易执行：检查与扣款在同一个写事务内完成，保证并发一致。"""
        if principal.role not in (ROLE_OPERATOR, ROLE_AUDITOR):
            raise PermissionDenied("仅交易运营员可以执行交易")
        amount, sources = self._normalize_request(request)
        now = self._now_iso()
        check_id = self._new_id("CHK")
        with self.repo.account_lock(account_id):
            self.repo.begin_write()
            try:
                snap = self._evaluate(account_id, amount, sources, now)
                if snap["decision"] == "approved":
                    new_balance = self.repo.adjust_account_balance(account_id, -amount)
                    for sid, used in sources.items():
                        self.repo.adjust_source_amount(account_id, sid, -used)
                    snap["total_balance_after"] = new_balance
                self.repo.insert_check({
                    "check_id": check_id,
                    "account_id": account_id,
                    "txn_ref": str(request.get("txn_ref", "")),
                    "checked_at": now,
                    "total_balance": snap.get("total_balance_after", snap["total_balance"]),
                    "frozen": snap["frozen"],
                    "available": snap["available"],
                    "request": {"amount": amount, "sources": sources},
                    "decision": snap["decision"],
                    "detail": snap["detail"],
                    "snapshot": snap,
                })
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"check_id": check_id, **snap}

    @staticmethod
    def _normalize_request(request: dict[str, Any]) -> tuple[int, dict[str, int]]:
        amount = request.get("amount")
        if not isinstance(amount, int) or amount <= 0:
            raise Conflict("交易金额必须为正整数")
        sources = request.get("sources") or {}
        if not isinstance(sources, dict):
            raise Conflict("sources 必须是 {来源批次: 金额} 映射")
        sources = {str(k): int(v) for k, v in sources.items() if int(v) != 0}
        if any(v <= 0 for v in sources.values()):
            raise Conflict("来源批次扣减金额必须为正")
        if sources and sum(sources.values()) != amount:
            raise Conflict("指定来源批次时，各批次金额之和必须等于交易金额")
        return amount, sources

    def _evaluate(
        self, account_id: str, amount: int, sources: dict[str, int], now: str
    ) -> dict:
        row = self.repo.get_account(account_id)
        if row is None:
            raise NotFound(f"账户不存在：{account_id}")
        snap = self._balance_snapshot(account_id, int(row["balance"]), now)
        reasons: list[str] = []
        if amount > snap["available"]:
            reasons.append(
                f"金额 {amount} 超过可用余额 {snap['available']}（冻结 {snap['frozen']}）"
            )
        for sid, used in sources.items():
            held = self.repo.get_source_amount(account_id, sid)
            frozen_src = snap["source_frozen"].get(sid, 0)
            if used > held - frozen_src:
                reasons.append(
                    f"批次 {sid} 拟用 {used}，可用仅 {max(0, held - frozen_src)}"
                )
        snap["decision"] = "approved" if not reasons else "rejected"
        snap["detail"] = "；".join(reasons)
        return snap

    def get_check(self, principal: Principal, check_id: str) -> dict:
        if principal.role not in (ROLE_OPERATOR, ROLE_ACCOUNTANT, ROLE_AUDITOR):
            raise PermissionDenied("无权查看交易检查记录")
        row = self.repo.get_check_row(check_id)
        if row is None:
            raise NotFound(f"检查记录不存在：{check_id}")
        return self._check_dict(row)

    def list_checks(
        self, principal: Principal, account_id: str | None = None
    ) -> list[dict]:
        if principal.role not in (ROLE_OPERATOR, ROLE_ACCOUNTANT, ROLE_AUDITOR):
            raise PermissionDenied("无权查看交易检查记录")
        return [self._check_dict(row) for row in self.repo.list_check_rows(account_id)]

    @staticmethod
    def _check_dict(row) -> dict:
        return {
            "check_id": row["check_id"],
            "account_id": row["account_id"],
            "txn_ref": row["txn_ref"],
            "checked_at": row["checked_at"],
            "total_balance": row["total_balance"],
            "frozen": row["frozen"],
            "available": row["available"],
            "request": json.loads(row["request_json"]),
            "decision": row["decision"],
            "detail": row["detail"],
            "snapshot": json.loads(row["snapshot_json"]),
        }

    # ---- 案件查询与证据隔离 ---------------------------------------------

    def list_cases(
        self, principal: Principal, account_id: str | None = None
    ) -> list[dict]:
        rows = self.repo.list_case_rows(account_id)
        return [self._case_dict(principal, row) for row in rows]

    def get_case(self, principal: Principal, case_id: str) -> dict:
        row = self._load_case_row(case_id)
        return self._case_dict(principal, row)

    def _case_visible(self, principal: Principal, row) -> bool:
        # 交易运营员可见冻结状态但不可见证据；申报员仅可见本人登记案件
        if principal.role == ROLE_FILER and row["created_by_name"] != principal.name:
            return False
        return True

    def _case_dict(self, principal: Principal, row) -> dict:
        if not self._case_visible(principal, row):
            raise NotFound("案件不存在或无权查看")
        versions = self.repo.list_versions(row["case_id"])
        entries = self.repo.list_entries(row["case_id"])
        data = {
            "case_id": row["case_id"],
            "account_id": row["account_id"],
            "title": row["title"],
            "status": row["status"],
            "current_version": row["current_version"],
            "created_by": {"role": row["created_by_role"], "name": row["created_by_name"]},
            "created_at": row["created_at"],
            "latest_scope": versions[-1].scope.to_dict() if versions else None,
            "expire_at": versions[-1].expire_at if versions else None,
            "versions": [v.to_dict() for v in versions],
            "entries": [e.to_dict() for e in entries],
        }
        # 案件证据隔离：仅授权角色可见证据引用
        authorized = principal.can_view_evidence and (
            principal.role != ROLE_FILER or row["created_by_name"] == principal.name
        )
        data["evidence_ref"] = row["evidence_ref"] if authorized else None
        return data

    def get_approval_chain(self, principal: Principal, case_id: str) -> dict:
        if not principal.can_view_evidence:
            raise PermissionDenied("审批链仅向授权角色开放")
        row = self._load_case_row(case_id)
        if principal.role == ROLE_FILER and row["created_by_name"] != principal.name:
            raise NotFound("案件不存在或无权查看")
        steps = self.repo.list_approval_steps(case_id)
        return {
            "case_id": case_id,
            "required_chain": self._chain_for(case_id),
            "steps": [s.to_dict() for s in steps],
        }

    def get_evidence(self, principal: Principal, case_id: str) -> dict:
        row = self._load_case_row(case_id)
        authorized = principal.can_view_evidence and (
            principal.role != ROLE_FILER or row["created_by_name"] == principal.name
        )
        if not authorized:
            # 与不存在统一处理，避免侧信道暴露案件是否存在
            raise NotFound("案件不存在或无权查看证据")
        ev = self.repo._conn.execute(
            "SELECT content, stored_at FROM case_evidence WHERE case_id=?", (case_id,)
        ).fetchone()
        return {
            "case_id": case_id,
            "evidence_ref": row["evidence_ref"],
            "content": ev["content"] if ev else "",
            "stored_at": ev["stored_at"] if ev else None,
        }

    # ---- 杂项 -----------------------------------------------------------

    @staticmethod
    def _parse_ts(value: str, field: str = "timestamp") -> datetime:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise Conflict(f"{field} 时间格式非法：{value}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _proposal_dict(row) -> dict:
        return {
            "proposal_id": row["proposal_id"],
            "case_id": row["case_id"],
            "action": row["action"],
            "payload": json.loads(row["payload_json"]),
            "requested_by": {
                "role": row["requested_by_role"],
                "name": row["requested_by_name"],
            },
            "status": row["status"],
            "chain_steps": json.loads(row["chain_steps_json"] or "[]"),
            "requested_at": row["requested_at"],
            "decided_at": row["decided_at"],
            "remark": row["remark"],
        }
