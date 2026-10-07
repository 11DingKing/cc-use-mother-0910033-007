"""SQLite 仓储层。

并发策略：

* 数据库启用 WAL，写事务一律 ``BEGIN IMMEDIATE`` —— SQLite 同一时刻只允许一个
  写事务，因此冻结变更与交易检查在数据库层面串行提交，不会出现交错提交；
* 进程内按账户加互斥锁，缩小同一账户操作的临界区竞争；
* ``freeze_entries`` / ``case_versions`` / ``approval_steps`` / ``balance_checks``
  全部只追加，不更新不删除，保证历史检查结果与审批链可追溯。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .models import (
    Action,
    ApprovalStep,
    Case,
    CaseStatus,
    CaseVersion,
    FreezeEntry,
    ScopeSpec,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    balance    INTEGER NOT NULL CHECK (balance >= 0),
    version    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sources (
    account_id TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (account_id, source_id)
);
CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    account_id       TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,
    current_version  INTEGER NOT NULL,
    evidence_ref     TEXT NOT NULL DEFAULT '',
    created_by_role  TEXT NOT NULL,
    created_by_name  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    lock_version     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cases_account ON cases(account_id);
CREATE TABLE IF NOT EXISTS case_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       TEXT NOT NULL,
    version_no    INTEGER NOT NULL,
    action        TEXT NOT NULL,
    status_after  TEXT NOT NULL,
    scope_json    TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    expire_at     TEXT,
    approver_role TEXT NOT NULL,
    approver_name TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    remark        TEXT NOT NULL DEFAULT '',
    UNIQUE (case_id, version_no)
);
CREATE TABLE IF NOT EXISTS freeze_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       TEXT NOT NULL,
    version_no    INTEGER NOT NULL,
    action        TEXT NOT NULL,
    amount_delta  INTEGER NOT NULL,
    source_id     TEXT,
    operator_role TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    approver      TEXT,
    created_at    TEXT NOT NULL,
    remark        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entries_case ON freeze_entries(case_id, version_no);
CREATE TABLE IF NOT EXISTS approval_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    action     TEXT,
    decided_at TEXT,
    remark     TEXT NOT NULL DEFAULT '',
    UNIQUE (case_id, seq)
);
CREATE TABLE IF NOT EXISTS balance_checks (
    check_id      TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    txn_ref       TEXT NOT NULL DEFAULT '',
    checked_at    TEXT NOT NULL,
    total_balance INTEGER NOT NULL,
    frozen        INTEGER NOT NULL,
    available     INTEGER NOT NULL,
    request_json  TEXT NOT NULL,
    decision      TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_account ON balance_checks(account_id, checked_at);
CREATE TABLE IF NOT EXISTS case_meta (
    case_id             TEXT PRIMARY KEY,
    approval_chain_json TEXT NOT NULL,
    effective_from      TEXT
);
CREATE TABLE IF NOT EXISTS case_evidence (
    case_id   TEXT PRIMARY KEY,
    content   TEXT NOT NULL DEFAULT '',
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_proposals (
    proposal_id       TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    action            TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    requested_by_role TEXT NOT NULL,
    requested_by_name TEXT NOT NULL,
    status            TEXT NOT NULL,
    chain_steps_json  TEXT NOT NULL DEFAULT '[]',
    requested_at      TEXT NOT NULL,
    decided_at        TEXT,
    remark            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proposals_case ON case_proposals(case_id);
"""

EFFECTIVE_STATUSES = (CaseStatus.CONFIRMED.value, CaseStatus.ACTIVE.value)


class Repository:
    """线程安全的 SQLite 仓储。

    每个线程持有独立连接（文件库各自连接；内存库通过 shared-cache URI 共享），
    另有一个常驻锚点连接保证内存库不被回收。
    """

    def __init__(self, dsn: str = ":memory:") -> None:
        if dsn == ":memory:":
            self._dsn = (
                f"file:freeze_mem_{uuid.uuid4().hex}?mode=memory&cache=shared",
                True,
            )
        else:
            Path(dsn).parent.mkdir(parents=True, exist_ok=True)
            self._dsn = (dsn, False)
        self._local = threading.local()
        self._anchor = self._new_connect()
        with self._anchor:
            self._anchor.executescript(SCHEMA)
        self._account_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _new_connect(self) -> sqlite3.Connection:
        target, is_uri = self._dsn
        conn = sqlite3.connect(
            target,
            check_same_thread=False,
            isolation_level=None,
            uri=is_uri,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        self._anchor.close()

    # ---- 锁与事务 -------------------------------------------------------

    def account_lock(self, account_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.RLock()
                self._account_locks[account_id] = lock
            return lock

    def begin_write(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def begin_read(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # ---- 账户与来源批次 -------------------------------------------------

    def upsert_account(self, account_id: str, balance: int) -> None:
        if balance < 0:
            raise ValueError("余额不能为负")
        self._conn.execute(
            "INSERT INTO accounts(account_id, balance, version) VALUES(?, ?, 1) "
            "ON CONFLICT(account_id) DO UPDATE SET balance=excluded.balance, "
            "version=accounts.version+1",
            (account_id, balance),
        )

    def upsert_source(self, account_id: str, source_id: str, amount: int) -> None:
        if amount < 0:
            raise ValueError("来源批次额度不能为负")
        self._conn.execute(
            "INSERT INTO sources(account_id, source_id, amount) VALUES(?, ?, ?) "
            "ON CONFLICT(account_id, source_id) DO UPDATE SET amount=excluded.amount",
            (account_id, source_id, amount),
        )

    def get_account(self, account_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()

    def get_source_amount(self, account_id: str, source_id: str) -> int:
        row = self._conn.execute(
            "SELECT amount FROM sources WHERE account_id=? AND source_id=?",
            (account_id, source_id),
        ).fetchone()
        return int(row["amount"]) if row else 0

    def adjust_account_balance(self, account_id: str, delta: int) -> int:
        row = self.get_account(account_id)
        if row is None:
            raise ValueError(f"账户不存在：{account_id}")
        new_balance = int(row["balance"]) + delta
        if new_balance < 0:
            raise ValueError("余额不能为负")
        self._conn.execute(
            "UPDATE accounts SET balance=?, version=version+1 WHERE account_id=?",
            (new_balance, account_id),
        )
        return new_balance

    def adjust_source_amount(self, account_id: str, source_id: str, delta: int) -> int:
        amount = self.get_source_amount(account_id, source_id) + delta
        if amount < 0:
            raise ValueError("来源批次额度不能为负")
        self.upsert_source(account_id, source_id, amount)
        return amount

    # ---- 案件 -----------------------------------------------------------

    def insert_case(self, case: Case) -> None:
        self._conn.execute(
            "INSERT INTO cases(case_id, account_id, title, status, current_version, "
            "evidence_ref, created_by_role, created_by_name, created_at, lock_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,0)",
            (
                case.case_id,
                case.account_id,
                case.title,
                case.status.value,
                case.current_version,
                case.evidence_ref,
                case.created_by_role,
                case.created_by_name,
                case.created_at,
            ),
        )

    def update_case_status(
        self, case_id: str, status: CaseStatus, current_version: int
    ) -> None:
        self._conn.execute(
            "UPDATE cases SET status=?, current_version=?, lock_version=lock_version+1 "
            "WHERE case_id=?",
            (status.value, current_version, case_id),
        )

    def get_case_row(self, case_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()

    def list_case_rows(self, account_id: str | None = None) -> list[sqlite3.Row]:
        if account_id is None:
            return list(self._conn.execute("SELECT * FROM cases ORDER BY created_at"))
        return list(
            self._conn.execute(
                "SELECT * FROM cases WHERE account_id=? ORDER BY created_at",
                (account_id,),
            )
        )

    # ---- 版本 / 分录 / 审批链 ------------------------------------------

    def insert_version(self, version: CaseVersion) -> None:
        self._conn.execute(
            "INSERT INTO case_versions(case_id, version_no, action, status_after, "
            "scope_json, effective_from, expire_at, approver_role, approver_name, "
            "created_at, remark) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                version.case_id,
                version.version_no,
                version.action.value,
                version.status_after.value,
                json.dumps(version.scope.to_dict(), ensure_ascii=False),
                version.effective_from,
                version.expire_at,
                version.approver_role,
                version.approver_name,
                version.created_at,
                version.remark,
            ),
        )

    def latest_version(self, case_id: str) -> CaseVersion | None:
        rows = self._conn.execute(
            "SELECT * FROM case_versions WHERE case_id=? ORDER BY version_no DESC LIMIT 1",
            (case_id,),
        ).fetchall()
        return _row_to_version(rows[0]) if rows else None

    def list_versions(self, case_id: str) -> list[CaseVersion]:
        rows = self._conn.execute(
            "SELECT * FROM case_versions WHERE case_id=? ORDER BY version_no",
            (case_id,),
        ).fetchall()
        return [_row_to_version(row) for row in rows]

    def insert_entry(self, entry: FreezeEntry) -> int:
        cur = self._conn.execute(
            "INSERT INTO freeze_entries(case_id, version_no, action, amount_delta, "
            "source_id, operator_role, operator_name, approver, created_at, remark) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry.case_id,
                entry.version_no,
                entry.action.value,
                entry.amount_delta,
                entry.source_id,
                entry.operator_role,
                entry.operator_name,
                entry.approver,
                entry.created_at,
                entry.remark,
            ),
        )
        return int(cur.lastrowid)

    def list_entries(self, case_id: str) -> list[FreezeEntry]:
        rows = self._conn.execute(
            "SELECT * FROM freeze_entries WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def insert_approval_step(self, step: ApprovalStep) -> None:
        self._conn.execute(
            "INSERT INTO approval_steps(case_id, seq, role, name, action, decided_at, remark) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                step.case_id,
                step.seq,
                step.role,
                step.name,
                step.action.value if step.action else None,
                step.decided_at,
                step.remark,
            ),
        )

    def list_approval_steps(self, case_id: str) -> list[ApprovalStep]:
        rows = self._conn.execute(
            "SELECT * FROM approval_steps WHERE case_id=? ORDER BY seq", (case_id,)
        ).fetchall()
        return [
            ApprovalStep(
                seq=int(row["seq"]),
                case_id=row["case_id"],
                role=row["role"],
                name=row["name"] or "",
                action=Action(row["action"]) if row["action"] else None,
                decided_at=row["decided_at"],
                remark=row["remark"] or "",
            )
            for row in rows
        ]

    def case_net_frozen(self, case_id: str) -> dict[str, Any]:
        """按分录求和案件净冻结：账户级金额与各来源批次金额。

        与案件当前状态/期限无关，供到期封存等需要在案件失效瞬间
        释放其占用额的场景使用。
        """
        rows = self._conn.execute(
            "SELECT source_id, COALESCE(SUM(amount_delta), 0) AS amt "
            "FROM freeze_entries WHERE case_id=? GROUP BY source_id",
            (case_id,),
        ).fetchall()
        account_amount = 0
        source_amounts: dict[str, int] = {}
        for row in rows:
            if row["source_id"] is None:
                account_amount = int(row["amt"])
            else:
                source_amounts[row["source_id"]] = int(row["amt"])
        return {"account_amount": account_amount, "source_amounts": source_amounts}

    # ---- 冻结汇总 -------------------------------------------------------

    def frozen_summary(self, account_id: str, now: str) -> dict[str, Any]:
        """汇总账户级与来源级有效冻结，含每个案件的版本快照。"""
        rows = self._conn.execute(
            "SELECT e.case_id, e.version_no, e.source_id, e.amount_delta, e.action, "
            "       c.current_version, v.expire_at "
            "FROM freeze_entries e "
            "JOIN cases c ON c.case_id = e.case_id "
            "JOIN case_versions v ON v.case_id = e.case_id "
            "  AND v.version_no = ("
            "      SELECT MAX(version_no) FROM case_versions WHERE case_id = e.case_id) "
            "WHERE c.account_id=? AND c.status IN (?, ?) "
            "AND (v.expire_at IS NULL OR v.expire_at >= ?)",
            (account_id, *EFFECTIVE_STATUSES, now),
        ).fetchall()
        account_frozen = 0
        source_frozen: dict[str, int] = {}
        per_case: dict[str, dict[str, Any]] = {}
        for row in rows:
            delta = int(row["amount_delta"])
            if row["source_id"] is None:
                account_frozen += delta
            else:
                source_frozen[row["source_id"]] = source_frozen.get(row["source_id"], 0) + delta
            bucket = per_case.setdefault(
                row["case_id"],
                {
                    "case_id": row["case_id"],
                    "version_no": int(row["current_version"]),
                    "expire_at": row["expire_at"],
                    "account_amount": 0,
                    "source_amounts": {},
                },
            )
            if row["source_id"] is None:
                bucket["account_amount"] += delta
            else:
                bucket["source_amounts"][row["source_id"]] = (
                    bucket["source_amounts"].get(row["source_id"], 0) + delta
                )
        source_total = sum(source_frozen.values())
        return {
            "account_frozen": account_frozen,
            "source_frozen": dict(sorted(source_frozen.items())),
            "source_frozen_total": source_total,
            "total_frozen": account_frozen + source_total,
            "cases": [per_case[key] for key in sorted(per_case)],
        }

    # ---- 历史检查 -------------------------------------------------------

    def insert_check(self, check: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO balance_checks(check_id, account_id, txn_ref, checked_at, "
            "total_balance, frozen, available, request_json, decision, detail, snapshot_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                check["check_id"],
                check["account_id"],
                check.get("txn_ref", ""),
                check["checked_at"],
                check["total_balance"],
                check["frozen"],
                check["available"],
                json.dumps(check["request"], ensure_ascii=False),
                check["decision"],
                check.get("detail", ""),
                json.dumps(check["snapshot"], ensure_ascii=False),
            ),
        )

    def get_check_row(self, check_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM balance_checks WHERE check_id=?", (check_id,)
        ).fetchone()

    def list_check_rows(self, account_id: str | None = None) -> list[sqlite3.Row]:
        if account_id is None:
            return list(
                self._conn.execute(
                    "SELECT * FROM balance_checks ORDER BY checked_at, check_id"
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM balance_checks WHERE account_id=? ORDER BY checked_at, check_id",
                (account_id,),
            )
        )

    def list_expired_case_ids(self, now: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT c.case_id FROM cases c "
            "JOIN case_versions v ON v.case_id = c.case_id "
            "  AND v.version_no = ("
            "      SELECT MAX(version_no) FROM case_versions WHERE case_id = c.case_id) "
            "WHERE c.status IN (?, ?) AND v.expire_at IS NOT NULL AND v.expire_at < ?",
            (*EFFECTIVE_STATUSES, now),
        ).fetchall()
        return [row["case_id"] for row in rows]

    def list_due_confirmations(self, now: str) -> list[str]:
        """已审批确认但生效时间未到、现已到生效时刻的案件。"""
        rows = self._conn.execute(
            "SELECT c.case_id FROM cases c "
            "JOIN case_versions v ON v.case_id = c.case_id "
            "  AND v.version_no = ("
            "      SELECT MAX(version_no) FROM case_versions WHERE case_id = c.case_id) "
            "WHERE c.status=? AND v.effective_from<=?",
            (CaseStatus.CONFIRMED.value, now),
        ).fetchall()
        return [row["case_id"] for row in rows]


def _row_to_version(row: sqlite3.Row) -> CaseVersion:
    return CaseVersion(
        version_no=int(row["version_no"]),
        case_id=row["case_id"],
        action=Action(row["action"]),
        status_after=CaseStatus(row["status_after"]),
        scope=ScopeSpec.from_dict(json.loads(row["scope_json"])),
        effective_from=row["effective_from"],
        expire_at=row["expire_at"],
        approver_role=row["approver_role"],
        approver_name=row["approver_name"],
        created_at=row["created_at"],
        remark=row["remark"] or "",
    )


def _row_to_entry(row: sqlite3.Row) -> FreezeEntry:
    return FreezeEntry(
        entry_id=int(row["id"]),
        case_id=row["case_id"],
        version_no=int(row["version_no"]),
        action=Action(row["action"]),
        amount_delta=int(row["amount_delta"]),
        source_id=row["source_id"],
        operator_role=row["operator_role"],
        operator_name=row["operator_name"],
        approver=row["approver"],
        created_at=row["created_at"],
        remark=row["remark"] or "",
    )
