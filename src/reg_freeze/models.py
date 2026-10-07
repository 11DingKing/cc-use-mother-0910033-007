"""领域模型与值对象。

所有金额均为整数积分，不使用浮点，避免并发与分录场景下的精度问题。
冻结作用域：

* ``account`` —— 整户/额度区间冻结；
* ``source``  —— 按来源批次条件冻结（与额度区间可叠加）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Scope(str, Enum):
    ACCOUNT = "account"
    SOURCE = "source"


class CaseStatus(str, Enum):
    DRAFT = "草稿"
    PENDING = "待核算"
    CONFIRMED = "已确认"
    ACTIVE = "执行中"
    SEALED = "已封存"


class Action(str, Enum):
    REGISTER = "登记"
    SUBMIT = "提交核算"
    APPROVE = "审批"
    REJECT = "驳回"
    EXPAND = "扩大"
    REDUCE = "缩减"
    RENEW = "续期"
    UNFREEZE = "解冻"
    EXPIRE = "到期"


@dataclass(frozen=True)
class ScopeSpec:
    """单个冻结作用域。

    * ``kind=account``：账户级额度冻结，``amount_limit`` 为最多冻结积分；
    * ``kind=source``：按来源批次条件冻结，``sources`` 为 ``(批次号, 上限)``
      序列，上限为 None 表示冻结该批次当前全额。
    """

    kind: Scope
    amount_limit: int | None = None
    sources: tuple[tuple[str, int | None], ...] = ()

    def __post_init__(self) -> None:
        if self.kind is Scope.ACCOUNT:
            if self.sources:
                raise ValueError("账户级冻结不能携带来源条件")
            if self.amount_limit is None or self.amount_limit < 0:
                raise ValueError("账户级冻结必须指定非负额度")
        else:
            if not self.sources:
                raise ValueError("来源条件冻结必须指定至少一个来源批次")
            if any((limit is not None) and limit < 0 for _, limit in self.sources):
                raise ValueError("来源批次冻结额度不能为负")

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(sid for sid, _ in self.sources)

    def source_limit(self, source_id: str) -> int | None:
        for sid, limit in self.sources:
            if sid == source_id:
                return limit
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "amount_limit": self.amount_limit,
            "sources": [[sid, limit] for sid, limit in self.sources],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ScopeSpec":
        raw_sources = value.get("sources")
        if raw_sources is None:
            # 向后兼容旧字段
            raw_sources = [[sid, None] for sid in value.get("source_ids") or ()]
        return cls(
            kind=Scope(value["kind"]),
            amount_limit=value.get("amount_limit"),
            sources=tuple((sid, limit) for sid, limit in raw_sources),
        )


@dataclass
class FreezeEntry:
    """一条冻结分录：不可变，登记每次冻结/扩大/缩减实际占用的额度。"""

    entry_id: int
    case_id: str
    version_no: int
    action: Action
    amount_delta: int
    source_id: str | None
    operator_role: str
    operator_name: str
    approver: str | None
    created_at: str
    remark: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "case_id": self.case_id,
            "version_no": self.version_no,
            "action": self.action.value,
            "amount_delta": self.amount_delta,
            "source_id": self.source_id,
            "operator_role": self.operator_role,
            "operator_name": self.operator_name,
            "approver": self.approver,
            "created_at": self.created_at,
            "remark": self.remark,
        }


@dataclass
class CaseVersion:
    """案件的一个审批版本。"""

    version_no: int
    case_id: str
    action: Action
    status_after: CaseStatus
    scope: ScopeSpec
    effective_from: str
    expire_at: str | None
    approver_role: str
    approver_name: str
    created_at: str
    remark: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "case_id": self.case_id,
            "action": self.action.value,
            "status_after": self.status_after.value,
            "scope": self.scope.to_dict(),
            "effective_from": self.effective_from,
            "expire_at": self.expire_at,
            "approver_role": self.approver_role,
            "approver_name": self.approver_name,
            "created_at": self.created_at,
            "remark": self.remark,
        }


@dataclass
class Case:
    case_id: str
    account_id: str
    status: CaseStatus
    current_version: int
    created_by_role: str
    created_by_name: str
    created_at: str
    title: str = ""
    evidence_ref: str = ""
    versions: list[CaseVersion] = field(default_factory=list)
    entries: list[FreezeEntry] = field(default_factory=list)


@dataclass
class ApprovalStep:
    """审批链中的一环。"""

    seq: int
    case_id: str
    role: str
    name: str
    action: Action | None
    decided_at: str | None
    remark: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "case_id": self.case_id,
            "role": self.role,
            "name": self.name,
            "action": self.action.value if self.action else None,
            "decided_at": self.decided_at,
            "remark": self.remark,
        }
