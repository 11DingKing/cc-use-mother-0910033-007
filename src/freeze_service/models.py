"""领域角色、状态与错误定义。"""
from __future__ import annotations

from dataclasses import dataclass

# 领域契约中的四类角色
ENTERPRISE = "企业申报员"
ACCOUNTING = "核算专员"
TRADING = "交易运营员"
AUDITOR = "监管审计员"

ROLES = (ENTERPRISE, ACCOUNTING, TRADING, AUDITOR)

# 有权查看案件证据的角色（案件证据隔离）
EVIDENCE_ROLES = frozenset({ACCOUNTING, AUDITOR})

# API 请求头 X-Role 使用的稳定角色代码（HTTP 头仅支持 latin-1）
ROLE_CODES = {
    "enterprise": ENTERPRISE,
    "accounting": ACCOUNTING,
    "trading": TRADING,
    "auditor": AUDITOR,
}

# 默认审批链：冻结登记、变更与解冻均需核算专员初审、监管审计员终审
DEFAULT_FREEZE_CHAIN = (ACCOUNTING, AUDITOR)
DEFAULT_AMEND_CHAIN = (ACCOUNTING, AUDITOR)

# 案件状态码 -> 领域契约状态名
CASE_STATES = {
    "draft": "草稿",
    "pending": "待核算",
    "rejected": "已驳回",
    "confirmed": "已确认",
    "active": "执行中",
    "sealed": "已封存",
}

FREEZE_STATES = ("pending", "active", "released", "expired")


class ServiceError(Exception):
    """业务规则错误，``status`` 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class NotFound(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 404)


class AuthError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 403)


@dataclass(frozen=True)
class Principal:
    actor_id: str
    role: str

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise AuthError(f"角色 {self.role} 无权执行该操作，需要：{'、'.join(roles)}")

    def can_see_evidence(self) -> bool:
        return self.role in EVIDENCE_ROLES
