"""SQLite 派生索引。

设计前提：**明细账（ledger/*.jsonl）是唯一真相，索引随时可以丢弃重建**。
索引只负责查询加速，不承担任何记账职责 —— 这正是"账实核对"能成立的前提。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import KIND_AUTHORIZATION, KIND_USAGE, canonical_json, parse_voucher_no

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vouchers (
    voucher_no               TEXT PRIMARY KEY,
    kind                     TEXT NOT NULL,
    seq                      INTEGER NOT NULL DEFAULT 0,
    year                     INTEGER NOT NULL DEFAULT 0,
    recorded_at              TEXT NOT NULL,
    occurred_at              TEXT,
    occurred_day             TEXT,
    agent_id                 TEXT,
    agent_provider           TEXT,
    model                    TEXT,
    session_id               TEXT,
    turn_id                  TEXT,
    project                  TEXT,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens  INTEGER NOT NULL DEFAULT 0,
    total_tokens             INTEGER NOT NULL DEFAULT 0,
    cost_amount              REAL,
    cost_currency            TEXT,
    cost_status              TEXT,
    budget_status            TEXT,
    budget_ids               TEXT,
    source_parser            TEXT,
    source_file              TEXT,
    dedup_key                TEXT,
    record_hash              TEXT NOT NULL,
    payload                  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vouchers_dedup
    ON vouchers (kind, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vouchers_occurred ON vouchers (occurred_day);
CREATE INDEX IF NOT EXISTS idx_vouchers_kind ON vouchers (kind);
CREATE INDEX IF NOT EXISTS idx_vouchers_agent ON vouchers (agent_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_model ON vouchers (model);
CREATE INDEX IF NOT EXISTS idx_vouchers_project ON vouchers (project);
CREATE INDEX IF NOT EXISTS idx_vouchers_session ON vouchers (session_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_budget ON vouchers (budget_status);
"""

INSERT_COLUMNS = (
    "voucher_no", "kind", "seq", "year", "recorded_at", "occurred_at", "occurred_day",
    "agent_id", "agent_provider", "model", "session_id", "turn_id", "project",
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "cost_amount", "cost_currency", "cost_status",
    "budget_status", "budget_ids", "source_parser", "source_file", "dedup_key",
    "record_hash", "payload",
)

GROUP_EXPRESSIONS = {
    "agent": "agent_id",
    "provider": "agent_provider",
    "model": "model",
    "project": "project",
    "day": "occurred_day",
    "session": "session_id",
    "parser": "source_parser",
    "budget_status": "budget_status",
    "cost_status": "cost_status",
    "voucher": "voucher_no",
}

FILTER_COLUMNS = {
    "agent": "agent_id",
    "provider": "agent_provider",
    "model": "model",
    "project": "project",
    "session": "session_id",
    "parser": "source_parser",
    "budget_status": "budget_status",
    "cost_status": "cost_status",
    "dedup_key": "dedup_key",
    "voucher_no": "voucher_no",
    "turn_id": "turn_id",
}

AGGREGATE_SELECT = """
    COUNT(*)                                                          AS vouchers,
    COALESCE(SUM(input_tokens), 0)                                    AS input_tokens,
    COALESCE(SUM(cached_input_tokens), 0)                             AS cached_input_tokens,
    COALESCE(SUM(cache_write_input_tokens), 0)                        AS cache_write_input_tokens,
    COALESCE(SUM(output_tokens), 0)                                   AS output_tokens,
    COALESCE(SUM(reasoning_output_tokens), 0)                         AS reasoning_output_tokens,
    COALESCE(SUM(total_tokens), 0)                                    AS total_tokens,
    COALESCE(SUM(CASE WHEN cost_status = 'priced' THEN cost_amount ELSE 0 END), 0)
                                                                     AS cost_amount,
    COALESCE(SUM(CASE WHEN cost_status = 'unpriced' THEN 1 ELSE 0 END), 0)
                                                                     AS unpriced_vouchers,
    COALESCE(SUM(CASE WHEN budget_status = 'exceeded' THEN 1 ELSE 0 END), 0)
                                                                     AS exceeded_vouchers,
    COALESCE(SUM(CASE WHEN budget_status = 'unbudgeted' THEN 1 ELSE 0 END), 0)
                                                                     AS unbudgeted_vouchers
"""


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_columns(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    agent = payload.get("agent") or {}
    session = payload.get("session") or {}
    cost = payload.get("cost") or {}
    budget = payload.get("budget") or {}
    source = payload.get("source") or {}
    return {
        "occurred_at": payload.get("occurred_at"),
        "occurred_day": payload.get("occurred_day"),
        "agent_id": agent.get("id"),
        "agent_provider": agent.get("provider"),
        "model": payload.get("model"),
        "session_id": session.get("id"),
        "turn_id": session.get("turn_id"),
        "project": payload.get("project"),
        "input_tokens": _as_int(usage.get("input_tokens")),
        "cached_input_tokens": _as_int(usage.get("cached_input_tokens")),
        "cache_write_input_tokens": _as_int(usage.get("cache_write_input_tokens")),
        "output_tokens": _as_int(usage.get("output_tokens")),
        "reasoning_output_tokens": _as_int(usage.get("reasoning_output_tokens")),
        "total_tokens": _as_int(usage.get("total_tokens")),
        "cost_amount": cost.get("amount"),
        "cost_currency": cost.get("currency"),
        "cost_status": cost.get("status"),
        "budget_status": budget.get("status"),
        "budget_ids": ",".join(budget.get("budget_ids") or []) or None,
        "source_parser": source.get("parser"),
        "source_file": source.get("file"),
    }


def _authorization_columns(payload: dict) -> dict:
    scope = payload.get("scope") or {}
    period = payload.get("period") or {}
    limit = payload.get("limit") or {}
    return {
        "occurred_at": period.get("from"),
        "occurred_day": period.get("from"),
        "agent_id": scope.get("agent"),
        "agent_provider": None,
        "model": scope.get("model"),
        "session_id": None,
        "turn_id": None,
        "project": scope.get("project"),
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "cost_amount": limit.get("amount"),
        "cost_currency": limit.get("currency"),
        "cost_status": "limit",
        "budget_status": None,
        "budget_ids": None,
        "source_parser": "authorization",
        "source_file": None,
    }


def build_where(
    *,
    kind: str | None = None,
    start: str | None = None,
    end: str | None = None,
    filters: dict | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if start:
        clauses.append("occurred_day >= ?")
        params.append(start)
    if end:
        clauses.append("occurred_day <= ?")
        params.append(end)
    for key, value in (filters or {}).items():
        if value in (None, ""):
            continue
        column = FILTER_COLUMNS.get(key)
        if column is None:
            raise ValueError("不支持的过滤字段: {0}".format(key))
        clauses.append("{0} = ?".format(column))
        params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


class Index:
    """vouchers 表的读写封装。所有写入都来自 Ledger.append / Ledger.reindex。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self._conn.commit()

    # -- 生命周期 --------------------------------------------------------- #
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- meta ------------------------------------------------------------- #
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # -- 凭证编号序列 ----------------------------------------------------- #
    def last_seq(self, kind: str, year: int) -> int:
        """索引里记录的「最后一个已用编号」。

        编号本身由明细账推导（见 Ledger.append）：索引不参与记账，
        因此不存在「索引与账本互相证明」的循环，这里只留一份便于对照。
        """
        return _as_int(self.get_meta("seq:{0}:{1}".format(kind, year), "0"))

    def set_seq(self, kind: str, year: int, seq: int) -> None:
        """记录「最后一个已用编号」（不是下一个可用编号）。"""
        self.set_meta("seq:{0}:{1}".format(kind, year), str(int(seq)))

    # -- 写入 ------------------------------------------------------------- #
    def add(self, record: dict, dedup_key: str | None = None) -> None:
        payload = record.get("payload") or {}
        kind = record.get("kind")
        parsed = parse_voucher_no(record.get("voucher_no"))
        row: dict[str, Any] = {
            "voucher_no": record.get("voucher_no"),
            "kind": kind,
            "seq": parsed[2] if parsed else 0,
            "year": parsed[1] if parsed else 0,
            "recorded_at": record.get("recorded_at") or "",
            "dedup_key": dedup_key,
            "record_hash": record.get("hash") or "",
            "payload": canonical_json(payload),
        }
        if kind == KIND_AUTHORIZATION:
            row.update(_authorization_columns(payload))
        else:
            row.update(_usage_columns(payload))
        for column in INSERT_COLUMNS:
            row.setdefault(column, None)
        placeholders = ", ".join(":" + column for column in INSERT_COLUMNS)
        columns = ", ".join(INSERT_COLUMNS)
        self._conn.execute(
            "INSERT INTO vouchers ({0}) VALUES ({1})".format(columns, placeholders),
            {column: row[column] for column in INSERT_COLUMNS},
        )
        self._conn.commit()

    def clear_vouchers(self) -> None:
        self._conn.execute("DELETE FROM vouchers")
        self._conn.execute("DELETE FROM meta WHERE key LIKE 'seq:%'")
        self._conn.commit()

    def has_dedup_key(self, kind: str, dedup_key: str) -> bool:
        if not dedup_key:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM vouchers WHERE kind = ? AND dedup_key = ? LIMIT 1",
            (kind, dedup_key),
        ).fetchone()
        return row is not None

    # -- 读取 ------------------------------------------------------------- #
    def counts(self) -> dict:
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM vouchers GROUP BY kind"
        ).fetchall()
        result = {KIND_USAGE: 0, KIND_AUTHORIZATION: 0}
        for row in rows:
            result[row["kind"]] = row["n"]
        result["total"] = sum(result.values())
        return result

    def record_hashes(self) -> dict:
        rows = self._conn.execute("SELECT voucher_no, record_hash FROM vouchers").fetchall()
        return {row["voucher_no"]: row["record_hash"] for row in rows}

    def query(
        self,
        *,
        kind: str | None = KIND_USAGE,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        order: str = "occurred_at ASC",
        filters: dict | None = None,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict]:
        selected = ", ".join(columns) if columns else (
            "voucher_no, kind, recorded_at, occurred_at, occurred_day, agent_id, agent_provider, "
            "model, session_id, turn_id, project, input_tokens, cached_input_tokens, "
            "cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens, "
            "cost_amount, cost_currency, cost_status, budget_status, budget_ids, "
            "source_parser, source_file, dedup_key"
        )
        where, params = build_where(kind=kind, start=start, end=end, filters=filters)
        sql = "SELECT {0} FROM vouchers{1} ORDER BY {2}".format(selected, where, order)
        if limit:
            sql += " LIMIT {0}".format(int(limit))
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def aggregate(
        self,
        *,
        group_by: str = "agent",
        kind: str | None = KIND_USAGE,
        start: str | None = None,
        end: str | None = None,
        filters: dict | None = None,
    ) -> list[dict]:
        expression = GROUP_EXPRESSIONS.get(group_by)
        if expression is None:
            raise ValueError("不支持的分组维度: {0}".format(group_by))
        where, params = build_where(kind=kind, start=start, end=end, filters=filters)
        sql = (
            "SELECT COALESCE({0}, '(未标注)') AS grp, {1} FROM vouchers{2} "
            "GROUP BY grp ORDER BY total_tokens DESC, grp ASC"
        ).format(expression, AGGREGATE_SELECT, where)
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def totals(
        self,
        *,
        kind: str | None = KIND_USAGE,
        start: str | None = None,
        end: str | None = None,
        filters: dict | None = None,
    ) -> dict:
        where, params = build_where(kind=kind, start=start, end=end, filters=filters)
        sql = "SELECT {0} FROM vouchers{1}".format(AGGREGATE_SELECT, where)
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else {}

    def authorizations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT voucher_no, payload FROM vouchers WHERE kind = ? ORDER BY voucher_no",
            (KIND_AUTHORIZATION,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({"voucher_no": row["voucher_no"], "payload": json.loads(row["payload"])})
        return result