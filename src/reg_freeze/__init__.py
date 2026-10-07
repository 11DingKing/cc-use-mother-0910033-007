"""监管冻结解冻服务端包。"""
from __future__ import annotations

from .security import Principal, DomainError, PermissionDenied, NotFound, Conflict
from .service import FreezeService, Repository
from .api import create_server

ROLE_FILER = "企业申报员"
ROLE_ACCOUNTANT = "核算专员"
ROLE_OPERATOR = "交易运营员"
ROLE_AUDITOR = "监管审计员"

STATE_DRAFT = "草稿"
STATE_PENDING = "待核算"
STATE_CONFIRMED = "已确认"
STATE_ACTIVE = "执行中"
STATE_SEALED = "已封存"

__all__ = [
    "Principal",
    "DomainError",
    "PermissionDenied",
    "NotFound",
    "Conflict",
    "FreezeService",
    "Repository",
    "create_server",
]
