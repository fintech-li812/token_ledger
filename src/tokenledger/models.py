"""凭证（voucher）的数据结构、编号规则与哈希链计算。

账本里每一条记录都是一张凭证：

    {"schema": 1, "voucher_no": "TL-2026-000001", "kind": "usage",
     "recorded_at": "...", "prev_hash": "...", "payload": {...}, "hash": "..."}

``hash`` 是对除 ``hash`` 以外的全部字段做规范化 JSON 后的 SHA-256；
``prev_hash`` 指向上一条凭证的 ``hash``，整本账因此是一条哈希链，
任何事后修改都会被 ``tledger verify`` 检出。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64

KIND_USAGE = "usage"
KIND_AUTHORIZATION = "authorization"
KIND_PREFIX = {KIND_USAGE: "TL", KIND_AUTHORIZATION: "TA"}
PREFIX_KIND = {prefix: kind for kind, prefix in KIND_PREFIX.items()}

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------- #
# 时间工具（账本内统一使用 UTC，展示时再换算）
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime | None = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: Any) -> datetime | None:
    """尽量宽容地解析时间串，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text[-1] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def date_str(value: Any) -> str | None:
    """取 UTC 日期（YYYY-MM-DD），用于按天分组与区间过滤。"""
    moment = value if isinstance(value, datetime) else parse_iso(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d") if moment else None


# --------------------------------------------------------------------------- #
# 规范化序列化与哈希
# --------------------------------------------------------------------------- #
def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_hash(record: dict) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    return sha256_text(canonical_json(body))


def seal(record: dict) -> dict:
    record["hash"] = record_hash(record)
    return record


def verify_hash(record: dict) -> bool:
    expected = record.get("hash")
    return bool(expected) and expected == record_hash(record)


def make_record(
    *,
    voucher_no: str,
    kind: str,
    recorded_at: str,
    prev_hash: str,
    payload: dict,
) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "voucher_no": voucher_no,
        "kind": kind,
        "recorded_at": recorded_at,
        "prev_hash": prev_hash,
        "payload": payload,
    }


def make_voucher_no(kind: str, year: int, seq: int) -> str:
    return "{0}-{1}-{2:06d}".format(KIND_PREFIX.get(kind, "TX"), year, seq)


def parse_voucher_no(voucher_no: Any) -> tuple[str, int, int] | None:
    parts = str(voucher_no or "").split("-")
    if len(parts) != 3:
        return None
    prefix, year, seq = parts
    try:
        return prefix, int(year), int(seq)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 用量
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict | None) -> "Usage":
        data = data or {}
        values = {}
        for field in USAGE_FIELDS:
            raw = data.get(field)
            try:
                values[field] = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                values[field] = 0
        return cls(**values)

    def to_dict(self) -> dict:
        return {field: getattr(self, field) for field in USAGE_FIELDS}

    @property
    def billable_input(self) -> int:
        return self.input_tokens

    @property
    def billable_output(self) -> int:
        return self.output_tokens

    def inferred_total(self) -> int:
        """total_tokens 缺失时按 input + output 兜底（reasoning 已含在 output 内）。"""
        return self.total_tokens or (self.billable_input + self.billable_output)

    def normalized(self) -> "Usage":
        """total_tokens 缺失时补上 input + output，保证所有汇总口径一致。"""
        if self.total_tokens:
            return self
        return Usage(
            input_tokens=self.input_tokens,
            cached_input_tokens=self.cached_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens,
            output_tokens=self.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens,
            total_tokens=self.inferred_total(),
        )

    def __bool__(self) -> bool:
        return any(getattr(self, field) for field in USAGE_FIELDS)


def sum_usage(items: Iterable[Usage]) -> Usage:
    values = dict.fromkeys(USAGE_FIELDS, 0)
    for item in items:
        for field in USAGE_FIELDS:
            values[field] += getattr(item, field)
    return Usage(**values)