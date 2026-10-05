"""监管冻结案件的 SQLite 存储。

使用 WAL + ``BEGIN IMMEDIATE``：写事务在进入时即获取保留锁，
并发的冻结变更与交易扣款会被数据库串行化，配合 busy_timeout 等待，
保证余额检查与扣款在同一个事务内完成。
连接按线程隔离（``check_same_thread=False`` + threading.local）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS freeze_cases (
    id            TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL,
    title         TEXT,
    reason        TEXT,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL,            -- draft/pending/active/rejected/released/expired
    freeze_chain  TEXT NOT NULL,            -- JSON: 审批角色链
    amend_chain   TEXT NOT NULL,            -- JSON: 变更/解冻审批角色链
    current_step  INTEGER NOT NULL DEFAULT 0,
    planned_from  TEXT,
    planned_to    TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_freeze (
    id             TEXT PRIMARY KEY,
    case_id        TEXT NOT NULL REFERENCES freeze_cases(id),
    amount         INTEGER NOT NULL,        -- 冻结积分（整数最小单位）
    source_batch   TEXT,                    -- 来源批次条件；NULL 表示不区分来源
    scope_key      TEXT,                    -- case_id|source_batch；NULL 表示整户额度冻结
    status         TEXT NOT NULL,           -- pending/active/released/expired
    effective_from TEXT,
    effective_to   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(case_id, scope_key)
);

CREATE TABLE IF NOT EXISTS case_cap (
    case_id      TEXT NOT NULL REFERENCES freeze_cases(id),
    scope_target TEXT,                        -- NULL 表示整户额度，否则 case_id|source_batch
    cap_amount   INTEGER NOT NULL,
    PRIMARY KEY(case_id, scope_target)
);

CREATE TABLE IF NOT EXISTS approval_steps (
    case_id    TEXT NOT NULL REFERENCES freeze_cases(id),
    round_no   INTEGER NOT NULL DEFAULT 1,
    step_no    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    decision   TEXT NOT NULL,              -- approved/rejected
    actor_id   TEXT NOT NULL,
    comment    TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(case_id, round_no, step_no)
);

CREATE TABLE IF NOT EXISTS case_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL REFERENCES freeze_cases(id),
    version_no INTEGER NOT NULL,
    action     TEXT NOT NULL,              -- created/submitted/approved_step/rejected/activated/...
    actor_id   TEXT,
    snapshot   TEXT NOT NULL,              -- 当时案件完整快照（JSON）
    parent_id  INTEGER REFERENCES case_versions(id),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version_no)
);

CREATE TABLE IF NOT EXISTS case_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        TEXT NOT NULL REFERENCES freeze_cases(id),
    freeze_id      TEXT REFERENCES case_freeze(id),
    entry_type     TEXT NOT NULL,          -- freeze_activate/freeze_expand/freeze_shrink/...
    amount_delta   INTEGER NOT NULL,       -- 带符号变动（释放/缩减为负）
    amount_after   INTEGER NOT NULL,
    effective_from TEXT,
    effective_to   TEXT,
    actor_id       TEXT,
    reason         TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_changes (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES freeze_cases(id),
    kind         TEXT NOT NULL,            -- amend / unfreeze
    payload      TEXT NOT NULL,            -- JSON
    status       TEXT NOT NULL,            -- pending/effective/rejected/cancelled
    current_step INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    created_by   TEXT NOT NULL,
    created_at   TEXT,
    decided_at   TEXT
);

CREATE TABLE IF NOT EXISTS change_steps (
    change_id  TEXT NOT NULL REFERENCES case_changes(id),
    step_no    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    decision   TEXT,
    actor_id   TEXT,
    comment    TEXT,
    decided_at TEXT,
    PRIMARY KEY(change_id, step_no)
);

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL UNIQUE,
    balance       INTEGER NOT NULL,
    version       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deposit_batches (
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    batch_no    TEXT NOT NULL,
    amount      INTEGER NOT NULL,           -- 该批次剩余可用（未被交易占用）积分
    PRIMARY KEY(account_id, batch_no)
);

CREATE TABLE IF NOT EXISTS holds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    txn_ref    TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    actor_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, txn_ref)
);

CREATE TABLE IF NOT EXISTS hold_allocations (
    hold_id  INTEGER NOT NULL REFERENCES holds(id),
    batch_no TEXT NOT NULL,                 -- NULL 表示账户在批次表建立前的未分桶余额
    amount   INTEGER NOT NULL,
    PRIMARY KEY(hold_id, batch_no)
);

CREATE TABLE IF NOT EXISTS balance_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    hold_id          INTEGER REFERENCES holds(id),
    txn_ref          TEXT,
    decision         TEXT NOT NULL,         -- passed/rejected
    requested_amount INTEGER NOT NULL,
    balance_before   INTEGER NOT NULL,
    frozen_total     INTEGER NOT NULL,
    available        INTEGER NOT NULL,
    snapshot         TEXT NOT NULL,         -- 当时冻结构成检查快照（JSON）
    checked_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_freeze_case ON case_freeze(case_id);
CREATE INDEX IF NOT EXISTS idx_entries_case ON case_entries(case_id);
CREATE INDEX IF NOT EXISTS idx_versions_case ON case_versions(case_id);
CREATE INDEX IF NOT EXISTS idx_checks_account ON balance_checks(account_id);
"""


class Store:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._tls = threading.local()
        self._mem_holder: sqlite3.Connection | None = None
        if self.path == ":memory:":
            # 内存库需要一个长连接保持数据，各线程通过它共享（由写锁串行保护）
            self._mem_holder = sqlite3.connect(self.path, check_same_thread=False)
        self._init_schema(self._connect())

    def _connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            assert self._mem_holder is not None
            conn = self._mem_holder
        else:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        # executescript 会自行处理事务边界（执行前先提交），无需显式 BEGIN
        conn.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """立即型写事务：进入即加锁，提交/回滚由本上下文负责。"""
        conn = self.conn()
        if self.path == ":memory:":
            # 共享内存连接无法跨线程并发，用线程锁等价串行化
            if not hasattr(self, "_mem_lock"):
                self._mem_lock = threading.RLock()
            self._mem_lock.acquire()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._mem_lock.release()
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
