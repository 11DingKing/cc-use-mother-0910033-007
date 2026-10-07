"""主体、角色权限与案件证据隔离。

角色（与 domain/contract.json 的 actors 保持一致）：

* 企业申报员：登记/起草冻结案件，提交核算；
* 核算专员：审批冻结生效、扩大/缩减/续期/解冻的核算环节；
* 交易运营员：执行交易、查看可用余额，但不可查看案件证据；
* 监管审计员：查看全部案件与证据、审计版本链与分录。

案件证据（evidence_ref、审批意见等）仅向案件相关角色与监管审计员开放，
交易运营员只能看到冻结对余额的影响，看不到证据内容。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Action

ROLE_FILER = "企业申报员"
ROLE_ACCOUNTANT = "核算专员"
ROLE_OPERATOR = "交易运营员"
ROLE_AUDITOR = "监管审计员"

ALL_ROLES = (ROLE_FILER, ROLE_ACCOUNTANT, ROLE_OPERATOR, ROLE_AUDITOR)

# 可查看案件证据的角色；交易运营员被显式排除（案件证据隔离）。
EVIDENCE_ROLES = frozenset({ROLE_FILER, ROLE_ACCOUNTANT, ROLE_AUDITOR})


class DomainError(Exception):
    """所有业务规则错误的基类。"""


class PermissionDenied(DomainError):
    """当前角色无权执行该操作或查看该资源。"""


class NotFound(DomainError):
    """案件或资源不存在（无权查看时也统一按不存在处理，避免侧信道）。"""


class Conflict(DomainError):
    """并发冲突或状态不允许该操作。"""


@dataclass(frozen=True)
class Principal:
    """经过鉴权的调用主体。"""

    user_id: str
    name: str
    role: str

    def __post_init__(self) -> None:
        if self.role not in ALL_ROLES:
            raise PermissionDenied(f"未知角色：{self.role}")

    @property
    def can_view_evidence(self) -> bool:
        return self.role in EVIDENCE_ROLES


# 各动作允许的发起角色矩阵（审批版本链）。
_ACTION_ROLES: dict[Action, frozenset[str]] = {
    Action.REGISTER: frozenset({ROLE_FILER}),
    Action.SUBMIT: frozenset({ROLE_FILER}),
    Action.APPROVE: frozenset({ROLE_ACCOUNTANT, ROLE_AUDITOR}),
    Action.REJECT: frozenset({ROLE_ACCOUNTANT, ROLE_AUDITOR}),
    Action.EXPAND: frozenset({ROLE_FILER, ROLE_ACCOUNTANT}),
    Action.REDUCE: frozenset({ROLE_FILER, ROLE_ACCOUNTANT}),
    Action.RENEW: frozenset({ROLE_FILER, ROLE_ACCOUNTANT}),
    Action.UNFREEZE: frozenset({ROLE_FILER, ROLE_ACCOUNTANT, ROLE_AUDITOR}),
}


def require_action(principal: Principal, action: Action) -> None:
    allowed = _ACTION_ROLES.get(action, frozenset())
    if principal.role not in allowed:
        raise PermissionDenied(
            f"角色 {principal.role} 无权执行动作 {action.value}"
        )
