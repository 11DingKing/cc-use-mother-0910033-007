"""HTTP API（标准库实现，无第三方依赖）。

鉴权：启动时内置/注册 Bearer Token -> 用户主体（角色）的映射，
请求需携带 ``Authorization: Bearer <token>``。

错误码约定：

* 403 角色无权操作；
* 404 资源不存在（无权查看证据时同样返回 404，避免侧信道）；
* 409 状态冲突/业务规则冲突/并发冲突。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .security import (
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    Principal,
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
)
from .service import FreezeService

# 演示用内置令牌；生产部署应通过 register_token 注入。
DEFAULT_TOKENS: dict[str, Principal] = {
    "token-filer": Principal("u-filer", "王申报", "企业申报员"),
    "token-accountant": Principal("u-accountant", "李核算", "核算专员"),
    "token-operator": Principal("u-operator", "赵运营", "交易运营员"),
    "token-auditor": Principal("u-auditor", "孙审计", "监管审计员"),
}


class _Auth:
    def __init__(self, tokens: dict[str, Principal]) -> None:
        self._tokens = dict(tokens)
        self._lock = threading.Lock()

    def register(self, token: str, principal: Principal) -> None:
        with self._lock:
            self._tokens[token] = principal

    def resolve(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise PermissionDenied("缺少 Bearer 令牌")
        token = header[len("Bearer "):].strip()
        with self._lock:
            principal = self._tokens.get(token)
        if principal is None:
            raise PermissionDenied("令牌无效")
        return principal


def create_server(
    service: FreezeService,
    host: str = "127.0.0.1",
    port: int = 8080,
    tokens: dict[str, Principal] | None = None,
) -> ThreadingHTTPServer:
    auth = _Auth(tokens if tokens is not None else DEFAULT_TOKENS)

    class Handler(BaseHTTPRequestHandler):
        server_version = "RegFreeze/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默
            return

        # ---- 工具 --------------------------------------------------------

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _principal(self) -> Principal:
            return auth.resolve(self.headers.get("Authorization"))

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise Conflict("请求体不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise Conflict("请求体必须是 JSON 对象")
            return value

        def _run(self, fn) -> None:
            try:
                principal = self._principal()
                result = fn(principal)
                self._send(200, {"ok": True, "data": result})
            except PermissionDenied as exc:
                self._send(403, {"ok": False, "error": "permission_denied", "message": str(exc)})
            except NotFound as exc:
                self._send(404, {"ok": False, "error": "not_found", "message": str(exc)})
            except Conflict as exc:
                self._send(409, {"ok": False, "error": "conflict", "message": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send(400, {"ok": False, "error": "bad_request", "message": str(exc)})
            except DomainError as exc:
                self._send(422, {"ok": False, "error": "domain_error", "message": str(exc)})

        # ---- 路由 --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            self._run(lambda p: self._dispatch_get(p, path))

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            self._run(lambda p: self._dispatch_post(p, path))

        def _dispatch_get(self, p: Principal, path: str) -> Any:
            svc = service
            if path == "/cases":
                return svc.list_cases(p)
            if path.startswith("/cases/"):
                cid = path.split("/")[2]
                sub = path.split("/")[3:]
                if not sub:
                    return svc.get_case(p, cid)
                if sub == ["approval-chain"]:
                    return svc.get_approval_chain(p, cid)
                if sub == ["evidence"]:
                    return svc.get_evidence(p, cid)
                if sub == ["amendments"]:
                    return svc.list_proposals(p, cid)
                raise NotFound(f"未知资源：{path}")
            if path.startswith("/amendments/"):
                return svc.get_proposal(p, path.split("/")[2])
            if path.startswith("/accounts/") and path.endswith("/balance"):
                return svc.available_balance(path.split("/")[2])
            if path.startswith("/accounts/") and path.split("/")[-1] == "checks":
                return svc.list_checks(p, path.split("/")[2])
            if path == "/checks":
                return svc.list_checks(p)
            if path.startswith("/checks/"):
                return svc.get_check(p, path.split("/")[2])
            raise NotFound(f"未知资源：{path}")

        def _dispatch_post(self, p: Principal, path: str) -> Any:
            svc = service
            body = self._body()
            parts = path.split("/")

            if path == "/admin/accounts":
                return svc.setup_account(p, body["account_id"], int(body["balance"]))
            if path == "/admin/sources":
                return svc.setup_source(
                    p, body["account_id"], body["source_id"], int(body["amount"])
                )
            if path == "/cases":
                kwargs: dict[str, Any] = {
                    "amount_limit": body.get("amount_limit"),
                    "sources": body.get("sources"),
                    "expire_at": body.get("expire_at"),
                    "effective_from": body.get("effective_from"),
                    "title": body.get("title", ""),
                    "evidence_ref": body.get("evidence_ref", ""),
                    "evidence_text": body.get("evidence_text", ""),
                    "case_id": body.get("case_id"),
                }
                if body.get("approval_chain"):
                    kwargs["approval_chain"] = body["approval_chain"]
                return svc.register_case(p, body["account_id"], **kwargs)
            if path.startswith("/cases/") and len(parts) == 3:
                raise NotFound(f"未知资源：{path}")
            if path.startswith("/cases/"):
                cid = parts[2]
                action = parts[3]
                if action == "submit":
                    return svc.submit_case(p, cid, body.get("remark", ""))
                if action == "approve":
                    return svc.approve_case(p, cid, body.get("remark", ""))
                if action == "reject":
                    return svc.reject_case(p, cid, body.get("remark", ""))
                if action == "emergency-unfreeze":
                    return svc.emergency_unfreeze(p, cid, body.get("remark", "监管紧急解冻"))
                if action == "amendments":
                    return svc.request_amendment(
                        p,
                        cid,
                        body["action"],
                        amount_delta=int(body.get("amount_delta", 0)),
                        source_deltas=body.get("source_deltas"),
                        expire_at=body.get("expire_at"),
                        extend_seconds=body.get("extend_seconds"),
                        remark=body.get("remark", ""),
                    )
                raise NotFound(f"未知资源：{path}")
            if path.startswith("/amendments/") and parts[-1] in ("approve", "reject"):
                return svc.decide_proposal(
                    p, parts[2], parts[-1] == "approve", body.get("remark", "")
                )
            if path.startswith("/accounts/") and parts[-1] == "checks":
                return svc.check_balance(p, parts[2], body)
            if path.startswith("/accounts/") and parts[-1] == "transactions":
                return svc.execute_transaction(p, parts[2], body)
            if path == "/sweep":
                if p.role not in (ROLE_ACCOUNTANT, ROLE_AUDITOR):
                    raise PermissionDenied("无权触发定时任务")
                return svc.sweep(body.get("at"))
            raise NotFound(f"未知资源：{path}")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.auth = auth  # type: ignore[attr-defined]
    return httpd
