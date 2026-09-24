"""定价：把 token 数换算成金额。

立场：**本项目不猜测任何厂商价格**。价格表默认是空的，
没配价格的凭证会明确标记为 ``unpriced``，绝不会被悄悄当成 0 元。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .models import Usage

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Rate:
    input: float = 0.0
    output: float = 0.0
    cached_input: float | None = None
    cache_write: float | None = None

    def resolved_cached_input(self) -> float:
        return self.input if self.cached_input is None else self.cached_input

    def resolved_cache_write(self) -> float:
        return self.input if self.cache_write is None else self.cache_write

    def to_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cached_input": self.resolved_cached_input(),
            "cache_write": self.resolved_cache_write(),
        }


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rate_from_dict(data: dict | None) -> Rate:
    data = data or {}
    return Rate(
        input=_number(data.get("input")) or 0.0,
        output=_number(data.get("output")) or 0.0,
        cached_input=_number(data.get("cached_input")),
        cache_write=_number(data.get("cache_write")),
    )


def resolve_rate(model: str | None, models: dict) -> tuple[str, Rate] | None:
    """先精确匹配，再按通配符匹配（如 "gpt-4*"）。"""
    if not models:
        return None
    name = model or "unknown"
    if name in models:
        return name, rate_from_dict(models[name])
    for pattern in sorted(models):
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(name, pattern):
            return pattern, rate_from_dict(models[pattern])
    return None


def price_usage(
    usage: Usage,
    model: str | None,
    models: dict,
    *,
    currency: str = "USD",
    cached_input_included: bool = True,
) -> dict:
    resolved = resolve_rate(model, models)
    if resolved is None:
        return {
            "status": "unpriced",
            "currency": currency,
            "amount": None,
            "reason": "未在配置中为模型 {0} 定价".format(model or "unknown"),
        }

    pattern, rate = resolved
    cached = usage.cached_input_tokens
    if cached_input_included:
        non_cached_input = max(usage.input_tokens - cached, 0)
    else:
        non_cached_input = usage.input_tokens
    amount = (
        non_cached_input * rate.input
        + cached * rate.resolved_cached_input()
        + usage.cache_write_input_tokens * rate.resolved_cache_write()
        + usage.output_tokens * rate.output
    ) / PER_MILLION

    return {
        "status": "priced",
        "currency": currency,
        "amount": round(amount, 6),
        "pricing_id": pattern,
        "unit": "per_million_tokens",
        "rates": rate.to_dict(),
        "billable": {
            "non_cached_input_tokens": non_cached_input,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": usage.cache_write_input_tokens,
            "output_tokens": usage.output_tokens,
        },
    }