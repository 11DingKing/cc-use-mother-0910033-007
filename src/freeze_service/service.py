"""监管冻结/解冻领域服务。

覆盖：
- 冻结案件登记（额度范围、来源批次条件、期限、审批链）；
- 审批链逐级批准，激活后才影响余额；
- 可用余额动态扣除生效冻结，区分整户额度与来源批次；
- 扩大/缩减/续期/解冻走审批，生效时生成案件版本与冻结分录；
- 交易在同一立即型事务内完成"检查+扣款"，冻结与交易并发串行化；
- 每笔成交保留当时的冻结检查结果快照；
- 到期批处理使过期冻结失效。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    ACCOUNTING,
    AUDITOR,
    DEFAULT_AMEND_CHAIN,
    DEFAULT_FREEZE_CHAIN,
    TRADING,
    AuthError,
    NotFound,
    Principal,
    ServiceError,
)
from .store import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class FreezeService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 账户

    def create_account(self, enterprise_id: str, balance: int = 0) -> dict:
        if balance < 0:
            raise ServiceError("初始余额不能为负")
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE enterprise_id=?", (enterprise_id,)
            ).fetchone()
            if row:
                raise ServiceError("企业账户已存在")
            account_id = _uid("acct")
            now = _iso(_now())
            conn.execute(
                "INSERT INTO accounts(id, enterprise_id, balance, version, created_at)"
                " VALUES(?,?,?,?,?)",
                (account_id, enterprise_id, balance, 0, now),
            )
            return self._account_dict(conn, account_id)

    def deposit(self, enterprise_id: str, amount: int, batch_no: str | None = None) -> dict:
        """充值；指定来源批次时登记批次池，供来源条件冻结使用。"""
        if amount <= 0:
            raise ServiceError("充值金额必须为正")
        with self.store.write() as conn:
            account_id = self._account_id(conn, enterprise_id)
            conn.execute(
                "UPDATE accounts SET balance=balance+?, version=version+1 WHERE id=?",
                (amount, account_id),
            )
            if batch_no is not None:
                conn.execute(
                    "INSERT INTO deposit_batches(account_id, batch_no, amount) VALUES(?,?,?)"
                    " ON CONFLICT(account_id, batch_no) DO UPDATE SET amount=amount+excluded.amount",
                    (account_id, batch_no, amount),
                )
            return self._account_dict(conn, account_id)

    def get_account(self, enterprise_id: str) -> dict:
        conn = self.store.conn()
        account_id = self._account_id(conn, enterprise_id)
        return self._account_dict(conn, account_id)

    def _account_id(self, conn, enterprise_id: str) -> str:
        row = conn.execute(
            "SELECT id FROM accounts WHERE enterprise_id=?", (enterprise_id,)
        ).fetchone()
        if not row:
            raise NotFound("企业账户不存在")
        return row["id"]

    def _account_dict(self, conn, account_id: str) -> dict:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise NotFound("账户不存在")
        batches = [
            {"batch_no": r["batch_no"], "amount": r["amount"]}
            for r in conn.execute(
                "SELECT batch_no, amount FROM deposit_batches WHERE account_id=? ORDER BY batch_no",
                (account_id,),
            )
        ]
        return {
            "account_id": row["id"],
            "enterprise_id": row["enterprise_id"],
            "balance": row["balance"],
            "version": row["version"],
            "batches": batches,
        }

    # ------------------------------------------------------------ 案件登记

    def register_case(self, principal: Principal, payload: dict) -> dict:
        """登记冻结案件草稿。

        payload 字段：
          enterprise_id, title, reason, evidence_refs(list[str]),
          freezes: [{amount, source_batch?}],  # amount>0；source_batch 缺省=整户额度
          effective_from?, effective_to?,      # 期限（ISO8601，UTC）
          freeze_chain?, amend_chain?          # 审批角色链
        """
        principal.require_role(*_case_creator_roles())
        enterprise_id = payload.get("enterprise_id")
        if not enterprise_id:
            raise ServiceError("缺少 enterprise_id")
        freezes = payload.get("freezes") or []
        if not freezes:
            raise ServiceError("至少登记一条冻结额度")
        norm_freezes: list[dict] = []
        seen_batches: set[str | None] = set()
        total = 0
        for item in freezes:
            amount = int(item.get("amount", 0))
            if amount <= 0:
                raise ServiceError("冻结额度必须为正整数")
            # 额度范围：当前冻结额 amount，可扩大上限 cap_amount（缺省等于当前额）
            cap_amount = int(item.get("cap_amount", amount))
            if cap_amount < amount:
                raise ServiceError("额度范围上限不能小于当前冻结额")
            batch = item.get("source_batch")
            if batch in seen_batches:
                raise ServiceError(f"来源批次 {batch} 重复登记")
            seen_batches.add(batch)
            total += amount
            norm_freezes.append(
                {"amount": amount, "cap_amount": cap_amount, "source_batch": batch}
            )
        effective_from = _parse_dt(payload.get("effective_from"))
        effective_to = _parse_dt(payload.get("effective_to"))
        if effective_to and effective_from and effective_to <= effective_from:
            raise ServiceError("到期时间必须晚于生效时间")

        freeze_chain = tuple(payload.get("freeze_chain") or DEFAULT_FREEZE_CHAIN)
        amend_chain = tuple(payload.get("amend_chain") or DEFAULT_AMEND_CHAIN)
        _validate_chain(freeze_chain)
        _validate_chain(amend_chain)

        now = _iso(_now())
        case_id = _uid("case")
        with self.store.write() as conn:
            account_id = self._account_id(conn, enterprise_id)
            conn.execute(
                "INSERT INTO freeze_cases(id, enterprise_id, title, reason, evidence_refs,"
                " status, freeze_chain, amend_chain, current_step, planned_from, planned_to,"
                " created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id, enterprise_id, payload.get("title"), payload.get("reason"),
                    json.dumps(payload.get("evidence_refs") or [], ensure_ascii=False),
                    "draft", json.dumps(freeze_chain), json.dumps(amend_chain), 0,
                    _iso(effective_from), _iso(effective_to), principal.actor_id, now, now,
                ),
            )
            for item in norm_freezes:
                scope_key = f"{case_id}|{item['source_batch']}" if item["source_batch"] else None
                fid = _uid("frz")
                conn.execute(
                    "INSERT INTO case_freeze(id, case_id, amount, source_batch, scope_key,"
                    " status, effective_from, effective_to, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid, case_id, item["amount"], item["source_batch"], scope_key,
                        "pending", _iso(effective_from), _iso(effective_to), now, now,
                    ),
                )
                conn.execute(
                    "INSERT INTO case_cap(case_id, scope_target, cap_amount) VALUES(?,?,?)",
                    (case_id, scope_key, item["cap_amount"]),
                )
            self._snapshot_version(conn, case_id, "created", principal.actor_id)
            return self._case_dict(conn, case_id, principal)

    def submit_case(self, principal: Principal, case_id: str) -> dict:
        with self.store.write() as conn:
            case = self._case_row(conn, case_id)
            principal.require_role(*_case_creator_roles())
            if case["created_by"] != principal.actor_id and principal.role not in (ACCOUNTING, AUDITOR):
                raise AuthError("只能提交本人登记的案件")
            if case["status"] != "draft":
                raise ServiceError("仅草稿状态可提交审批")
            self._update_case_status(conn, case_id, "pending")
            self._snapshot_version(conn, case_id, "submitted", principal.actor_id)
            return self._case_dict(conn, case_id, principal)

    def approve_case_step(self, principal: Principal, case_id: str, comment: str = "") -> dict:
        """审批冻结登记链的当前步骤。全部批准后案件激活。"""
        with self.store.write() as conn:
            case = self._case_row(conn, case_id)
            if case["status"] not in ("pending",):
                raise ServiceError("案件不在待审批状态")
            chain = json.loads(case["freeze_chain"])
            step = case["current_step"]
            if step >= len(chain):
                raise ServiceError("审批链已完成")
            if principal.role != chain[step]:
                raise AuthError(f"当前第 {step + 1} 步需 {chain[step]} 审批")
            now = _iso(_now())
            conn.execute(
                "INSERT INTO approval_steps(case_id, round_no, step_no, role, decision,"
                " actor_id, comment, decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, 1, step, principal.role, "approved", principal.actor_id, comment, now),
            )
            step += 1
            conn.execute(
                "UPDATE freeze_cases SET current_step=?, updated_at=? WHERE id=?",
                (step, now, case_id),
            )
            if step >= len(chain):
                self._activate_case(conn, case_id)
                action = "activated"
            else:
                action = "approved_step"
            self._snapshot_version(conn, case_id, action, principal.actor_id)
            return self._case_dict(conn, case_id, principal)

    def reject_case(self, principal: Principal, case_id: str, comment: str = "") -> dict:
        with self.store.write() as conn:
            case = self._case_row(conn, case_id)
            if case["status"] != "pending":
                raise ServiceError("案件不在待审批状态")
            chain = json.loads(case["freeze_chain"])
            step = case["current_step"]
            if step >= len(chain) or principal.role != chain[step]:
                raise AuthError("当前审批角色才能驳回")
            now = _iso(_now())
            conn.execute(
                "INSERT INTO approval_steps(case_id, round_no, step_no, role, decision,"
                " actor_id, comment, decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, 1, step, principal.role, "rejected", principal.actor_id, comment, now),
            )
            self._update_case_status(conn, case_id, "rejected")
            self._snapshot_version(conn, case_id, "rejected", principal.actor_id)
            return self._case_dict(conn, case_id, principal)

    def _activate_case(self, conn, case_id: str, at: datetime | None = None) -> None:
        """审批通过：在有效期内激活，生成激活分录；未到生效时间则先确认。"""
        case = self._case_row(conn, case_id)
        now = at or _now()
        now_s = _iso(now)
        planned_from = _parse_dt(case["planned_from"])
        planned_to = _parse_dt(case["planned_to"])
        if planned_to and planned_to <= now:
            self._update_case_status(conn, case_id, "sealed")
            conn.execute(
                "UPDATE case_freeze SET status='expired', updated_at=? WHERE case_id=?",
                (now_s, case_id),
            )
            return
        if planned_from and planned_from > now:
            self._update_case_status(conn, case_id, "confirmed")
            conn.execute(
                "UPDATE case_freeze SET status='confirmed', updated_at=? WHERE case_id=?",
                (now_s, case_id),
            )
            return
        self._update_case_status(conn, case_id, "active")
        for frz in conn.execute("SELECT * FROM case_freeze WHERE case_id=?", (case_id,)):
            conn.execute(
                "UPDATE case_freeze SET status='active', updated_at=? WHERE id=?",
                (now_s, frz["id"]),
            )
            conn.execute(
                "INSERT INTO case_entries(case_id, freeze_id, entry_type, amount_delta,"
                " amount_after, effective_from, effective_to, actor_id, reason, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id, frz["id"], "freeze_activate", frz["amount"], frz["amount"],
                    frz["effective_from"], frz["effective_to"], None, "审批通过激活冻结", now_s,
                ),
            )

    # ------------------------------------------------------- 扩大/缩减/续期

    def request_change(
        self,
        principal: Principal,
        case_id: str,
        kind: str,
        payload: dict,
        reason: str = "",
    ) -> dict:
        """登记变更申请。kind=amend（扩大/缩减/续期）或 unfreeze（解冻）。

        amend payload: {"amounts": {source_batch 或 "": new_amount},
                        "effective_from"?, "effective_to"?}
          - 同批次新额度 > 当前 = 扩大；< 当前 = 缩减；
          - effective_to 晚于当前到期 = 续期。
        unfreeze payload: {"freeze_ids"?: [...]}；缺省为整案解冻。
        """
        principal.require_role(ACCOUNTING, AUDITOR)
        if kind not in ("amend", "unfreeze"):
            raise ServiceError("未知变更类型")
        with self.store.write() as conn:
            case = self._case_row(conn, case_id)
            if case["status"] not in ("active", "confirmed"):
                raise ServiceError("仅执行中/已确认案件可申请变更或解冻")
            if kind == "amend":
                payload = self._normalize_amend(conn, case_id, payload)
            else:
                payload = self._normalize_unfreeze(conn, case_id, payload)
            change_id = _uid("chg")
            now = _iso(_now())
            chain = json.loads(case["amend_chain"])
            conn.execute(
                "INSERT INTO case_changes(id, case_id, kind, payload, status, current_step,"
                " reason, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    change_id, case_id, kind, json.dumps(payload, ensure_ascii=False),
                    "pending", 0, reason, principal.actor_id, now,
                ),
            )
            for i, role in enumerate(chain):
                conn.execute(
                    "INSERT INTO change_steps(change_id, step_no, role) VALUES(?,?,?)",
                    (change_id, i, role),
                )
            self._snapshot_version(conn, case_id, f"{kind}_requested", principal.actor_id)
            return self._change_dict(conn, change_id)

    def _normalize_amend(self, conn, case_id: str, payload: dict) -> dict:
        amounts = payload.get("amounts") or {}
        current = {
            (r["source_batch"] if r["source_batch"] is not None else ""): r["amount"]
            for r in conn.execute("SELECT source_batch, amount FROM case_freeze WHERE case_id=?", (case_id,))
        }
        norm: dict[str, int] = {}
        for key, raw in amounts.items():
            value = int(raw)
            if value < 0:
                raise ServiceError("目标冻结额度不能为负")
            if key != "" and key not in current:
                raise ServiceError(f"案件不含来源批次 {key}，不能新增冻结范围")
            scope_target = None if key == "" else f"{case_id}|{key}"
            cap_row = conn.execute(
                "SELECT cap_amount FROM case_cap WHERE case_id=? AND scope_target IS ?",
                (case_id, scope_target),
            ).fetchone()
            if cap_row and value > cap_row["cap_amount"]:
                raise ServiceError(
                    f"扩大后的冻结 {value} 超过登记额度范围 {cap_row['cap_amount']}"
                )
            norm[key] = value
        out: dict[str, Any] = {"amounts": norm}
        if "effective_from" in payload or "effective_to" in payload:
            ef = _parse_dt(payload.get("effective_from"))
            et = _parse_dt(payload.get("effective_to"))
            if et and ef and et <= ef:
                raise ServiceError("到期时间必须晚于生效时间")
            out["effective_from"] = _iso(ef)
            out["effective_to"] = _iso(et)
        return out

    def _normalize_unfreeze(self, conn, case_id: str, payload: dict) -> dict:
        ids = payload.get("freeze_ids")
        if ids:
            found = {
                r["id"] for r in conn.execute(
                    "SELECT id FROM case_freeze WHERE case_id=? AND status IN ('active','confirmed')",
                    (case_id,),
                )
            }
            unknown = set(ids) - found
            if unknown:
                raise ServiceError("待解冻冻结项不存在或已释放：" + "、".join(sorted(unknown)))
            return {"freeze_ids": list(ids)}
        return {"freeze_ids": None}

    def approve_change(self, principal: Principal, change_id: str, comment: str = "") -> dict:
        with self.store.write() as conn:
            change = self._change_row(conn, change_id)
            if change["status"] != "pending":
                raise ServiceError("变更申请不在待审批状态")
            case = self._case_row(conn, change["case_id"])
            chain = json.loads(case["amend_chain"])
            step = change["current_step"]
            if principal.role != chain[step]:
                raise AuthError(f"当前第 {step + 1} 步需 {chain[step]} 审批")
            now = _iso(_now())
            conn.execute(
                "UPDATE change_steps SET decision='approved', actor_id=?, comment=?,"
                " decided_at=? WHERE change_id=? AND step_no=?",
                (principal.actor_id, comment, now, change_id, step),
            )
            step += 1
            conn.execute("UPDATE case_changes SET current_step=? WHERE id=?", (step, change_id))
            if step < len(chain):
                return self._change_dict(conn, change_id)
            # 终审通过：应用变更
            conn.execute(
                "UPDATE case_changes SET status='effective', decided_at=? WHERE id=?",
                (now, change_id),
            )
            data = json.loads(change["payload"])
            if change["kind"] == "amend":
                self._apply_amend(conn, change["case_id"], data, principal.actor_id)
            else:
                self._apply_unfreeze(conn, change["case_id"], data, principal.actor_id)
            self._snapshot_version(
                conn, change["case_id"], f"{change['kind']}_effective", principal.actor_id
            )
            return self._change_dict(conn, change_id)

    def reject_change(self, principal: Principal, change_id: str, comment: str = "") -> dict:
        with self.store.write() as conn:
            change = self._change_row(conn, change_id)
            if change["status"] != "pending":
                raise ServiceError("变更申请不在待审批状态")
            case = self._case_row(conn, change["case_id"])
            chain = json.loads(case["amend_chain"])
            step = change["current_step"]
            if principal.role != chain[step]:
                raise AuthError("当前审批角色才能驳回")
            now = _iso(_now())
            conn.execute(
                "UPDATE change_steps SET decision='rejected', actor_id=?, comment=?,"
                " decided_at=? WHERE change_id=? AND step_no=?",
                (principal.actor_id, comment, now, change_id, step),
            )
            conn.execute(
                "UPDATE case_changes SET status='rejected', decided_at=? WHERE id=?",
                (now, change_id),
            )
            self._snapshot_version(
                conn, change["case_id"], f"{change['kind']}_rejected", principal.actor_id
            )
            return self._change_dict(conn, change_id)

    def _apply_amend(self, conn, case_id: str, data: dict, actor_id: str) -> None:
        now = _now()
        now_s = _iso(now)
        amounts = data.get("amounts") or {}
        rows = {
            (r["source_batch"] if r["source_batch"] is not None else ""): r
            for r in conn.execute("SELECT * FROM case_freeze WHERE case_id=?", (case_id,))
        }
        for key, new_amount in amounts.items():
            row = rows[key]
            old_amount = row["amount"]
            if new_amount == old_amount:
                continue
            delta = new_amount - old_amount
            entry_type = "freeze_expand" if delta > 0 else "freeze_shrink"
            conn.execute(
                "UPDATE case_freeze SET amount=?, updated_at=? WHERE id=?",
                (new_amount, now_s, row["id"]),
            )
            conn.execute(
                "INSERT INTO case_entries(case_id, freeze_id, entry_type, amount_delta,"
                " amount_after, effective_from, effective_to, actor_id, reason, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id, row["id"], entry_type, delta, new_amount,
                    row["effective_from"], row["effective_to"], actor_id,
                    "扩大冻结" if delta > 0 else "缩减冻结", now_s,
                ),
            )
        if "effective_to" in data or "effective_from" in data:
            new_from = data.get("effective_from")
            new_to = data.get("effective_to")
            if new_to and _parse_dt(new_to) <= now:
                raise ServiceError("续期后的到期时间已过")
            conn.execute(
                "UPDATE case_freeze SET effective_from=COALESCE(?, effective_from),"
                " effective_to=COALESCE(?, effective_to), updated_at=? WHERE case_id=?",
                (new_from, new_to, now_s, case_id),
            )
            conn.execute(
                "UPDATE freeze_cases SET planned_from=COALESCE(?, planned_from),"
                " planned_to=COALESCE(?, planned_to), updated_at=? WHERE id=?",
                (new_from, new_to, now_s, case_id),
            )
            conn.execute(
                "INSERT INTO case_entries(case_id, freeze_id, entry_type, amount_delta,"
                " amount_after, effective_from, effective_to, actor_id, reason, created_at)"
                " SELECT ?, id, 'freeze_renew', 0, amount, ?, ?, ?, '续期冻结期限', ?"
                " FROM case_freeze WHERE case_id=?",
                (case_id, new_from, new_to, actor_id, now_s, case_id),
            )

    def _apply_unfreeze(self, conn, case_id: str, data: dict, actor_id: str) -> None:
        now = _now()
        now_s = _iso(now)
        ids = data.get("freeze_ids")
        query = "SELECT * FROM case_freeze WHERE case_id=? AND status IN ('active','confirmed')"
        params: tuple = (case_id,)
        if ids is not None:
            query += f" AND id IN ({','.join('?' * len(ids))})"
            params = (case_id, *ids)
        targets = list(conn.execute(query, params))
        if not targets:
            raise ServiceError("没有可解冻的冻结项")
        for row in targets:
            conn.execute(
                "UPDATE case_freeze SET status='released', amount=0, updated_at=? WHERE id=?",
                (now_s, row["id"]),
            )
            conn.execute(
                "INSERT INTO case_entries(case_id, freeze_id, entry_type, amount_delta,"
                " amount_after, effective_from, effective_to, actor_id, reason, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id, row["id"], "freeze_release", -row["amount"], 0,
                    row["effective_from"], row["effective_to"], actor_id, "审批解冻", now_s,
                ),
            )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM case_freeze WHERE case_id=? AND status IN ('active','confirmed')",
            (case_id,),
        ).fetchone()["n"]
        if remaining == 0:
            self._update_case_status(conn, case_id, "sealed", now)

    # ------------------------------------------------------------- 到期处理

    def sweep_expired(self, at: datetime | None = None) -> dict:
        """使过期冻结失效，确认态到达生效时间的案件激活。返回处理数量。"""
        now = at or _now()
        now_s = _iso(now)
        activated = 0
        expired = 0
        with self.store.write() as conn:
            for case in conn.execute("SELECT * FROM freeze_cases WHERE status IN ('confirmed','active')"):
                planned_from = _parse_dt(case["planned_from"])
                planned_to = _parse_dt(case["planned_to"])
                if case["status"] == "confirmed" and (not planned_from or planned_from <= now):
                    self._activate_case(conn, case["id"], now)
                    activated += 1
                    continue
                if planned_to and planned_to <= now and case["status"] == "active":
                    for row in conn.execute(
                        "SELECT * FROM case_freeze WHERE case_id=? AND status='active'", (case["id"],)
                    ):
                        conn.execute(
                            "UPDATE case_freeze SET status='expired', amount=0, updated_at=? WHERE id=?",
                            (now_s, row["id"]),
                        )
                        conn.execute(
                            "INSERT INTO case_entries(case_id, freeze_id, entry_type, amount_delta,"
                            " amount_after, effective_from, effective_to, actor_id, reason, created_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                case["id"], row["id"], "freeze_expire", -row["amount"], 0,
                                row["effective_from"], row["effective_to"], None,
                                "冻结到期自动失效", now_s,
                            ),
                        )
                    self._update_case_status(conn, case["id"], "sealed", now)
                    expired += 1
        return {"activated": activated, "expired": expired}

    # ------------------------------------------------------------- 余额计算

    def available_balance(self, enterprise_id: str, at: datetime | None = None) -> dict:
        """动态计算可用余额：账户余额扣除当前有效冻结。

        - 整户冻结按 min(额度, 剩余账户余额) 计入；
        - 来源批次冻结按 min(批次冻结额度, 该批次剩余) 计入；
        - 同一批次多案件取并集上限（受批次剩余约束）。
        """
        now = at or _now()
        conn = self.store.conn()
        account_id = self._account_id(conn, enterprise_id)
        return self._available(conn, account_id, now)

    def _effective_freezes(self, conn, account_id: str, now: datetime) -> list:
        enterprise = conn.execute(
            "SELECT enterprise_id FROM accounts WHERE id=?", (account_id,)
        ).fetchone()["enterprise_id"]
        result = []
        rows = conn.execute(
            "SELECT f.* FROM case_freeze f JOIN freeze_cases c ON c.id=f.case_id"
            " WHERE c.enterprise_id=? AND f.status='active'",
            (enterprise,),
        ).fetchall()
        for r in rows:
            ef = _parse_dt(r["effective_from"])
            et = _parse_dt(r["effective_to"])
            if ef and ef > now:
                continue
            if et and et <= now:
                continue
            result.append(r)
        return result

    def _freeze_cover(self, conn, account_id: str, now: datetime) -> dict:
        """计算当前有效冻结对资金覆盖情况。

        - 批次冻结按 min(最大案件额度, 批次剩余) 覆盖该批次；
        - 整户冻结覆盖未被批次冻结覆盖的资金（未分桶 + 批次未覆盖部分）；
        - 同批次多案件取最大额度（上限并集），避免重复扣除。
        """
        acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        balance = acct["balance"]
        batch_remaining = {
            r["batch_no"]: r["amount"]
            for r in conn.execute(
                "SELECT batch_no, amount FROM deposit_batches WHERE account_id=?", (account_id,)
            )
        }
        unbucketed = balance - sum(batch_remaining.values())

        active = self._effective_freezes(conn, account_id, now)
        general_amounts: list[int] = []
        batch_amounts: dict[str, int] = {}
        detail = []
        for r in active:
            cap = conn.execute(
                "SELECT cap_amount FROM case_cap WHERE case_id=? AND scope_target IS ?",
                (r["case_id"], r["scope_key"]),
            ).fetchone()
            cap_amount = cap["cap_amount"] if cap else r["amount"]
            detail.append({
                "case_id": r["case_id"],
                "freeze_id": r["id"],
                "source_batch": r["source_batch"],
                "amount": r["amount"],
                "cap_amount": cap_amount,
                "effective_from": r["effective_from"],
                "effective_to": r["effective_to"],
            })
            # 余额扣除按当前冻结额；额度范围上限只约束扩大。
            # 多案件对同一批次的冻结是相互独立的法律冻结，累加并封顶于批次剩余。
            if r["source_batch"] is None:
                general_amounts.append(r["amount"])
            else:
                batch_amounts[r["source_batch"]] = (
                    batch_amounts.get(r["source_batch"], 0) + r["amount"]
                )

        batch_breakdown: dict[str, int] = {}
        for batch, amount_sum in batch_amounts.items():
            remaining = batch_remaining.get(batch, 0)
            batch_breakdown[batch] = min(amount_sum, max(0, remaining))
        frozen_batch = sum(batch_breakdown.values())
        if general_amounts:
            uncovered = max(0, unbucketed) + sum(
                max(0, batch_remaining.get(b, 0) - batch_breakdown.get(b, 0))
                for b in batch_remaining
            )
            general_hold = sum(general_amounts)
            frozen_general = min(general_hold, uncovered)
        else:
            general_hold = 0
            frozen_general = 0
        frozen_total = min(max(0, balance), frozen_batch + frozen_general)
        return {
            "balance": balance,
            "batch_remaining": batch_remaining,
            "unbucketed": unbucketed,
            "batch_breakdown": dict(sorted(batch_breakdown.items())),
            "frozen_batch": frozen_batch,
            "frozen_general": frozen_general,
            "frozen_total": frozen_total,
            "detail": detail,
        }

    def _available(self, conn, account_id: str, now: datetime) -> dict:
        cover = self._freeze_cover(conn, account_id, now)
        acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return {
            "enterprise_id": acct["enterprise_id"],
            "balance": cover["balance"],
            "frozen_total": cover["frozen_total"],
            "available": cover["balance"] - cover["frozen_total"],
            "frozen_general": cover["frozen_general"],
            "frozen_by_batch": cover["batch_breakdown"],
            "active_freezes": cover["detail"],
            "checked_at": _iso(now),
        }

    # ---------------------------------------------------------------- 交易

    def check_balance(self, principal: Principal, enterprise_id: str, amount: int,
                      txn_ref: str | None = None) -> dict:
        """只检查不扣款，返回当时检查结果（含冻结快照）。"""
        principal.require_role(TRADING, AUDITOR, ACCOUNTING)
        if amount <= 0:
            raise ServiceError("交易金额必须为正")
        conn = self.store.conn()
        account_id = self._account_id(conn, enterprise_id)
        now = _now()
        avail = self._available(conn, account_id, now)
        decision = "passed" if avail["available"] >= amount else "rejected"
        snapshot = json.dumps(
            {"freezes": avail["active_freezes"], "frozen_by_batch": avail["frozen_by_batch"],
             "frozen_general": avail["frozen_general"]},
            ensure_ascii=False,
        )
        cur = conn.execute(
            "INSERT INTO balance_checks(account_id, hold_id, txn_ref, decision,"
            " requested_amount, balance_before, frozen_total, available, snapshot, checked_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (account_id, None, txn_ref, decision, amount, avail["balance"],
             avail["frozen_total"], avail["available"], snapshot, _iso(now)),
        )
        return {"check_id": cur.lastrowid, "decision": decision, **avail}

    def execute_transaction(self, principal: Principal, enterprise_id: str,
                            amount: int, txn_ref: str) -> dict:
        """交易扣款：检查与扣款在同一个立即型事务内原子完成。

        并发的冻结变更与交易在数据库层串行化；幂等键 txn_ref 防重。
        检查结果（含当时冻结快照）随成交记录永久保留。
        """
        principal.require_role(TRADING)
        if amount <= 0:
            raise ServiceError("交易金额必须为正")
        now = _now()
        now_s = _iso(now)
        with self.store.write() as conn:
            account_id = self._account_id(conn, enterprise_id)
            dup = conn.execute(
                "SELECT id FROM holds WHERE account_id=? AND txn_ref=?", (account_id, txn_ref)
            ).fetchone()
            if dup:
                raise ServiceError("交易流水号重复，禁止重复扣款", status=409)
            cover = self._freeze_cover(conn, account_id, now)
            available = cover["balance"] - cover["frozen_total"]
            snapshot = json.dumps(
                {"freezes": cover["detail"], "frozen_by_batch": cover["batch_breakdown"],
                 "frozen_general": cover["frozen_general"]},
                ensure_ascii=False,
            )
            if available < amount:
                conn.execute(
                    "INSERT INTO balance_checks(account_id, hold_id, txn_ref, decision,"
                    " requested_amount, balance_before, frozen_total, available, snapshot, checked_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (account_id, None, txn_ref, "rejected", amount, cover["balance"],
                     cover["frozen_total"], available, snapshot, now_s),
                )
                rejection = ServiceError(
                    f"可用余额不足：需要 {amount}，可用 {available}"
                    f"（余额 {cover['balance']}，冻结 {cover['frozen_total']}）"
                )
            else:
                cur = conn.execute(
                    "INSERT INTO holds(account_id, txn_ref, amount, actor_id, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (account_id, txn_ref, amount, principal.actor_id, now_s),
                )
                hold_id = cur.lastrowid
                conn.execute(
                    "UPDATE accounts SET balance=balance-?, version=version+1 WHERE id=?",
                    (amount, account_id),
                )
                self._consume_batches(conn, account_id, amount, hold_id, cover)
                conn.execute(
                    "INSERT INTO balance_checks(account_id, hold_id, txn_ref, decision,"
                    " requested_amount, balance_before, frozen_total, available, snapshot, checked_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (account_id, hold_id, txn_ref, "passed", amount, cover["balance"],
                     cover["frozen_total"], available, snapshot, now_s),
                )
                rejection = None
        if rejection is not None:
            # 拒绝记录已随事务提交保留；向调用方抛出业务错误
            raise rejection
        return {
            "txn_ref": txn_ref,
            "hold_id": hold_id,
            "amount": amount,
            "balance_before": cover["balance"],
            "frozen_at_check": cover["frozen_total"],
            "available_at_check": available,
            "balance_after": cover["balance"] - amount,
            "check_snapshot": json.loads(snapshot),
            "checked_at": now_s,
        }

    def _consume_batches(self, conn, account_id: str, amount: int, hold_id: int,
                         cover: dict) -> None:
        """扣减批次剩余并记录占用分配。

        只允许耗用各批次中未被来源冻结覆盖的部分；整户冻结保护的额度由
        总耗用上限（available = 资金池 - frozen_general）保证不被穿透；
        仍不足的部分由未分桶（历史）余额承担。
        """
        frozen_by_batch = cover["batch_breakdown"]
        need = amount
        rows = conn.execute(
            "SELECT batch_no, amount FROM deposit_batches WHERE account_id=? AND amount>0"
            " ORDER BY batch_no",
            (account_id,),
        ).fetchall()
        for r in rows:
            if need <= 0:
                break
            free_in_batch = r["amount"] - frozen_by_batch.get(r["batch_no"], 0)
            take = min(free_in_batch, need)
            if take <= 0:
                continue
            conn.execute(
                "UPDATE deposit_batches SET amount=amount-? WHERE account_id=? AND batch_no=?",
                (take, account_id, r["batch_no"]),
            )
            conn.execute(
                "INSERT INTO hold_allocations(hold_id, batch_no, amount) VALUES(?,?,?)",
                (hold_id, r["batch_no"], take),
            )
            need -= take
        # need > 0 时由未分桶余额承担；可用余额检查已保证其充足

    def transaction_history(self, principal: Principal, enterprise_id: str) -> list[dict]:
        """历史成交及其当时检查结果（保留冻结快照）。"""
        principal.require_role(TRADING, AUDITOR, ACCOUNTING)
        conn = self.store.conn()
        account_id = self._account_id(conn, enterprise_id)
        out = []
        for r in conn.execute(
            "SELECT h.*, bc.snapshot AS check_snapshot, bc.balance_before,"
            " bc.frozen_total, bc.available AS available_at_check, bc.checked_at"
            " FROM holds h JOIN balance_checks bc ON bc.hold_id=h.id"
            " WHERE h.account_id=? ORDER BY h.id",
            (account_id,),
        ):
            out.append({
                "txn_ref": r["txn_ref"],
                "amount": r["amount"],
                "balance_before": r["balance_before"],
                "frozen_at_check": r["frozen_total"],
                "available_at_check": r["available_at_check"],
                "check_snapshot": json.loads(r["check_snapshot"]),
                "checked_at": r["checked_at"],
            })
        return out

    def balance_checks(self, principal: Principal, enterprise_id: str) -> list[dict]:
        """所有余额检查记录（含被拒绝交易），保留当时检查结果。"""
        principal.require_role(TRADING, AUDITOR, ACCOUNTING)
        conn = self.store.conn()
        account_id = self._account_id(conn, enterprise_id)
        return [
            {
                "check_id": r["id"],
                "txn_ref": r["txn_ref"],
                "decision": r["decision"],
                "requested_amount": r["requested_amount"],
                "balance_before": r["balance_before"],
                "frozen_total": r["frozen_total"],
                "available": r["available"],
                "snapshot": json.loads(r["snapshot"]),
                "checked_at": r["checked_at"],
            }
            for r in conn.execute(
                "SELECT * FROM balance_checks WHERE account_id=? ORDER BY id", (account_id,)
            )
        ]

    # --------------------------------------------------------------- 查询面

    def get_case(self, principal: Principal, case_id: str) -> dict:
        conn = self.store.conn()
        return self._case_dict(conn, case_id, principal)

    def list_cases(self, principal: Principal, enterprise_id: str | None = None) -> list[dict]:
        conn = self.store.conn()
        query = "SELECT id FROM freeze_cases"
        params: tuple = ()
        if enterprise_id is not None:
            query += " WHERE enterprise_id=?"
            params = (enterprise_id,)
        query += " ORDER BY created_at"
        return [self._case_dict(conn, r["id"], principal)
                for r in conn.execute(query, params)]

    def case_versions(self, principal: Principal, case_id: str) -> list[dict]:
        """审批版本链：每一版本含案件完整快照。"""
        self._case_row(self.store.conn(), case_id)
        rows = self.store.conn().execute(
            "SELECT * FROM case_versions WHERE case_id=? ORDER BY version_no", (case_id,)
        ).fetchall()
        return [
            {
                "version_no": r["version_no"],
                "action": r["action"],
                "actor_id": r["actor_id"],
                "created_at": r["created_at"],
                "snapshot": json.loads(r["snapshot"]),
            }
            for r in rows
        ]

    def case_entries(self, principal: Principal, case_id: str) -> list[dict]:
        """冻结分录：激活/扩大/缩减/续期/解冻/到期的逐笔带符号变动。"""
        self._case_row(self.store.conn(), case_id)
        rows = self.store.conn().execute(
            "SELECT * FROM case_entries WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_change(self, principal: Principal, change_id: str) -> dict:
        return self._change_dict(self.store.conn(), change_id)

    # -------------------------------------------------------------- 内部装配

    def _case_row(self, conn, case_id: str):
        row = conn.execute("SELECT * FROM freeze_cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise NotFound("冻结案件不存在")
        return row

    def _change_row(self, conn, change_id: str):
        row = conn.execute("SELECT * FROM case_changes WHERE id=?", (change_id,)).fetchone()
        if not row:
            raise NotFound("变更申请不存在")
        return row

    def _update_case_status(self, conn, case_id: str, status: str, at: datetime | None = None) -> None:
        conn.execute(
            "UPDATE freeze_cases SET status=?, updated_at=? WHERE id=?",
            (status, _iso(at or _now()), case_id),
        )

    def _next_version_no(self, conn, case_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM case_versions WHERE case_id=?",
            (case_id,),
        ).fetchone()
        return row["n"]

    def _snapshot_version(self, conn, case_id: str, action: str, actor_id: str | None) -> None:
        case = self._case_row(conn, case_id)
        parent_row = conn.execute(
            "SELECT id FROM case_versions WHERE case_id=? ORDER BY version_no DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        freezes = [
            {k: r[k] for k in ("id", "amount", "source_batch", "status",
                               "effective_from", "effective_to")}
            for r in conn.execute("SELECT * FROM case_freeze WHERE case_id=?", (case_id,))
        ]
        snapshot = {
            "case_id": case["id"],
            "enterprise_id": case["enterprise_id"],
            "status": case["status"],
            "title": case["title"],
            "reason": case["reason"],
            "freezes": freezes,
            "planned_from": case["planned_from"],
            "planned_to": case["planned_to"],
            "current_step": case["current_step"],
        }
        conn.execute(
            "INSERT INTO case_versions(case_id, version_no, action, actor_id, snapshot,"
            " parent_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                case_id, self._next_version_no(conn, case_id), action, actor_id,
                json.dumps(snapshot, ensure_ascii=False),
                parent_row["id"] if parent_row else None, _iso(_now()),
            ),
        )

    def _case_dict(self, conn, case_id: str, principal: Principal) -> dict:
        case = self._case_row(conn, case_id)
        freeze_rows = conn.execute(
            "SELECT * FROM case_freeze WHERE case_id=?", (case_id,)
        ).fetchall()
        freezes = []
        for r in freeze_rows:
            cap = conn.execute(
                "SELECT cap_amount FROM case_cap WHERE case_id=? AND scope_target IS ?",
                (case_id, r["scope_key"]),
            ).fetchone()
            freezes.append({
                "freeze_id": r["id"],
                "amount": r["amount"],
                "source_batch": r["source_batch"],
                "status": r["status"],
                "effective_from": r["effective_from"],
                "effective_to": r["effective_to"],
                "cap_amount": cap["cap_amount"],
            })
        approvals = [
            {
                "round_no": r["round_no"],
                "step_no": r["step_no"],
                "role": r["role"],
                "decision": r["decision"],
                "actor_id": r["actor_id"],
                "comment": r["comment"],
                "decided_at": r["decided_at"],
            }
            for r in conn.execute(
                "SELECT * FROM approval_steps WHERE case_id=? ORDER BY round_no, step_no",
                (case_id,),
            )
        ]
        evidence = json.loads(case["evidence_refs"])
        result = {
            "case_id": case["id"],
            "enterprise_id": case["enterprise_id"],
            "status": case["status"],
            "title": case["title"],
            "reason": case["reason"],
            "freezes": freezes,
            "planned_from": case["planned_from"],
            "planned_to": case["planned_to"],
            "current_step": case["current_step"],
            "freeze_chain": json.loads(case["freeze_chain"]),
            "amend_chain": json.loads(case["amend_chain"]),
            "approval_steps": approvals,
            "created_by": case["created_by"],
            "created_at": case["created_at"],
            "updated_at": case["updated_at"],
            # 案件证据隔离：仅授权角色可见证据明细，其他角色仅见是否存在
            "evidence": evidence if principal.can_see_evidence() else None,
            "evidence_count": len(evidence),
            "evidence_visible": principal.can_see_evidence(),
        }
        return result

    def _change_dict(self, conn, change_id: str) -> dict:
        row = self._change_row(conn, change_id)
        steps = [
            {
                "step_no": r["step_no"],
                "role": r["role"],
                "decision": r["decision"],
                "actor_id": r["actor_id"],
                "comment": r["comment"],
                "decided_at": r["decided_at"],
            }
            for r in conn.execute(
                "SELECT * FROM change_steps WHERE change_id=? ORDER BY step_no", (change_id,)
            )
        ]
        return {
            "change_id": row["id"],
            "case_id": row["case_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "current_step": row["current_step"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "steps": steps,
        }


def _case_creator_roles() -> tuple[str, ...]:
    from .models import ENTERPRISE
    return (ENTERPRISE, ACCOUNTING, AUDITOR)


def _validate_chain(chain: Iterable[str]) -> None:
    chain = tuple(chain)
    if not chain:
        raise ServiceError("审批链不能为空")
    from .models import ROLES
    for role in chain:
        if role not in ROLES:
            raise ServiceError(f"审批链含未知角色：{role}")
