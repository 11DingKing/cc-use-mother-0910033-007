"""基于标准库 ``http.server`` 的 JSON API。

鉴权（演示用，请求头传入）：
  X-Actor-Id: 操作人标识
  X-Role:     企业申报员 | 核算专员 | 交易运营员 | 监管审计员

案件证据仅核算专员、监管审计员可见；其他角色读取案件时 evidence=null。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .models import ROLE_CODES, AuthError, Principal, ServiceError
from .service import FreezeService
from .store import Store


def _json_default(obj):
    return str(obj)


class ApiHandler(BaseHTTPRequestHandler):
    service: FreezeService  # 由 make_server 注入到类属性

    def log_message(self, fmt: str, *args) -> None:  # 静默
        return

    # ------------------------------------------------------------ 基础收发

    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _principal(self) -> Principal:
        actor = self.headers.get("X-Actor-Id", "").strip()
        code = self.headers.get("X-Role", "").strip()
        if not actor or not code:
            raise AuthError("缺少请求头 X-Actor-Id / X-Role")
        role = ROLE_CODES.get(code)
        if role is None:
            raise AuthError(
                f"未知角色代码：{code}；可选 {', '.join(ROLE_CODES)}"
            )
        return Principal(actor_id=actor, role=role)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}", status=400)
        if not isinstance(value, dict):
            raise ServiceError("请求体必须是 JSON 对象", status=400)
        return value

    def _handle(self, fn) -> None:
        try:
            result = fn()
        except AuthError as exc:
            self._send(exc.status, {"error": str(exc)})
        except ServiceError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免泄漏堆栈
            self._send(500, {"error": f"服务器内部错误：{exc}"})
        else:
            if result is None:
                self._send(200, {"ok": True})
            else:
                self._send(200, result)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(lambda: self._route("GET"))

    def do_POST(self) -> None:  # noqa: N802
        self._handle(lambda: self._route("POST"))

    # -------------------------------------------------------------- 路由

    def _route(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        principal = self._principal()
        body = self._body() if method == "POST" else {}
        svc = self.service

        if method == "POST" and path == "/accounts":
            return svc.create_account(body["enterprise_id"], int(body.get("balance", 0)))

        m = re.fullmatch(r"/accounts/([^/]+)/deposits", path)
        if method == "POST" and m:
            return svc.deposit(m.group(1), int(body["amount"]), body.get("batch_no"))

        m = re.fullmatch(r"/accounts/([^/]+)", path)
        if method == "GET" and m:
            return svc.get_account(m.group(1))

        m = re.fullmatch(r"/accounts/([^/]+)/available", path)
        if method == "GET" and m:
            return svc.available_balance(m.group(1))

        m = re.fullmatch(r"/accounts/([^/]+)/checks", path)
        if method == "POST" and m:
            return svc.check_balance(
                principal, m.group(1), int(body["amount"]), body.get("txn_ref")
            )

        m = re.fullmatch(r"/accounts/([^/]+)/transactions", path)
        if method == "POST" and m:
            return svc.execute_transaction(
                principal, m.group(1), int(body["amount"]), body["txn_ref"]
            )
        if method == "GET" and m:
            return svc.transaction_history(principal, m.group(1))

        m = re.fullmatch(r"/accounts/([^/]+)/checks", path)
        if method == "GET" and m:
            return svc.balance_checks(principal, m.group(1))

        if method == "POST" and path == "/cases":
            return svc.register_case(principal, body)
        if method == "GET" and path == "/cases":
            return svc.list_cases(principal, query.get("enterprise_id", [None])[0])

        m = re.fullmatch(r"/cases/([^/]+)", path)
        if method == "GET" and m:
            return svc.get_case(principal, m.group(1))

        m = re.fullmatch(r"/cases/([^/]+)/submit", path)
        if method == "POST" and m:
            return svc.submit_case(principal, m.group(1))

        m = re.fullmatch(r"/cases/([^/]+)/approve", path)
        if method == "POST" and m:
            return svc.approve_case_step(principal, m.group(1), body.get("comment", ""))

        m = re.fullmatch(r"/cases/([^/]+)/reject", path)
        if method == "POST" and m:
            return svc.reject_case(principal, m.group(1), body.get("comment", ""))

        m = re.fullmatch(r"/cases/([^/]+)/versions", path)
        if method == "GET" and m:
            return svc.case_versions(principal, m.group(1))

        m = re.fullmatch(r"/cases/([^/]+)/entries", path)
        if method == "GET" and m:
            return svc.case_entries(principal, m.group(1))

        m = re.fullmatch(r"/cases/([^/]+)/changes", path)
        if method == "POST" and m:
            return svc.request_change(
                principal, m.group(1), body["kind"], body.get("payload", {}),
                body.get("reason", ""),
            )

        m = re.fullmatch(r"/changes/([^/]+)/approve", path)
        if method == "POST" and m:
            return svc.approve_change(principal, m.group(1), body.get("comment", ""))

        m = re.fullmatch(r"/changes/([^/]+)/reject", path)
        if method == "POST" and m:
            return svc.reject_change(principal, m.group(1), body.get("comment", ""))

        m = re.fullmatch(r"/changes/([^/]+)", path)
        if method == "GET" and m:
            return svc.get_change(principal, m.group(1))

        if method == "POST" and path == "/admin/sweep-expired":
            principal.require_role("监管审计员", "核算专员")
            return svc.sweep_expired()

        self._send(404, {"error": f"未找到路由：{method} {path}"})
        return None


def make_server(host: str, port: int, db_path: str = ":memory:") -> ThreadingHTTPServer:
    store = Store(db_path)
    service = FreezeService(store)

    handler = ApiHandler
    handler.service = service
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="监管冻结/解冻服务端")
    parser.add_argument("--host", default=os.environ.get("FREEZE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FREEZE_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("FREEZE_DB", "freeze.sqlite3"))
    args = parser.parse_args()
    httpd = make_server(args.host, args.port, args.db)
    print(f"监管冻结服务监听 http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
