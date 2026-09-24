"""外部对账：把账本里的用量与供应商账单逐项比对。

列名是宽容的（支持常见别名），因为各厂商的账单格式不统一。
任何一边多出来的记录都会被明确报出来，而不是取交集了事。
"""

from __future__ import annotations

import csv
from pathlib import Path

from .models import KIND_USAGE

ALIASES = {
    "model": ("model", "model_name", "模型"),
    "agent": ("agent", "agent_id", "agent_name"),
    "date": ("date", "day", "usage_date", "occurred_day", "日期"),
    "input_tokens": ("input_tokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "output"),
    "total_tokens": ("total_tokens", "total"),
    "amount": ("amount", "cost", "cost_amount", "金额"),
    "currency": ("currency", "货币"),
}


class ReconcileError(RuntimeError):
    """账单无法解析。"""


def _pick(row: dict, key: str):
    lowered = {str(column).strip().lower(): value for column, value in row.items()}
    for alias in ALIASES[key]:
        if alias in lowered:
            return lowered[alias]
    return None


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def read_bill(path: str | Path) -> list[dict]:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ReconcileError("账单文件不存在：{0}".format(path))
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            row = {
                "model": _pick(raw, "model"),
                "agent": _pick(raw, "agent"),
                "date": _pick(raw, "date"),
                "input_tokens": _number(_pick(raw, "input_tokens")),
                "output_tokens": _number(_pick(raw, "output_tokens")),
                "total_tokens": _number(_pick(raw, "total_tokens")),
                "amount": _number(_pick(raw, "amount")),
                "currency": _pick(raw, "currency"),
            }
            if row["date"]:
                row["date"] = str(row["date"]).strip()[:10]
            if row["total_tokens"] is None:
                parts = [row["input_tokens"], row["output_tokens"]]
                row["total_tokens"] = sum(value for value in parts if value is not None) or None
            rows.append(row)
    if not rows:
        raise ReconcileError("账单里没有可用的数据行：{0}".format(path))
    return rows


def _within_tolerance(ledger_value, bill_value, *, amount=0.0, ratio=0.0) -> bool:
    if ledger_value is None or bill_value is None:
        return False
    diff = abs(ledger_value - bill_value)
    if diff <= amount:
        return True
    base = max(abs(bill_value), abs(ledger_value), 1e-9)
    return (diff / base) <= ratio


def compare(
    ledger,
    bill_rows: list[dict],
    *,
    tolerance_amount: float = 0.0,
    tolerance_ratio: float = 0.0,
) -> dict:
    dates = sorted(row["date"] for row in bill_rows if row.get("date"))
    start = dates[0] if dates else None
    end = dates[-1] if dates else None

    filters = {}
    models = sorted({row["model"] for row in bill_rows if row.get("model")})
    agents = sorted({row["agent"] for row in bill_rows if row.get("agent")})
    if len(agents) == 1:
        filters["agent"] = agents[0]

    ledger_rows = ledger.index.aggregate(
        group_by="model", kind=KIND_USAGE, start=start, end=end, filters=filters
    )
    ledger_by_model = {str(row.get("grp")): row for row in ledger_rows}

    results = []
    matched_models = set()
    for row in bill_rows:
        model = row.get("model") or "(未标注)"
        entry = ledger_by_model.get(model)
        if entry is None:
            results.append(
                {
                    "model": model,
                    "date": row.get("date") or "-",
                    "status": "missing_in_ledger",
                    "bill_tokens": row.get("total_tokens"),
                    "ledger_tokens": None,
                    "token_diff": None,
                    "bill_amount": row.get("amount"),
                    "ledger_amount": None,
                    "amount_diff": None,
                }
            )
            continue
        matched_models.add(model)
        token_ok = _within_tolerance(
            float(entry.get("total_tokens") or 0),
            float(row.get("total_tokens") or 0),
            amount=0.0,
            ratio=tolerance_ratio,
        )
        amount_ok = _within_tolerance(
            float(entry.get("cost_amount") or 0),
            float(row.get("amount") or 0),
            amount=tolerance_amount,
            ratio=tolerance_ratio,
        )
        status = "ok" if (token_ok and amount_ok) else "diff"
        if float(entry.get("cost_amount") or 0) == 0 and int(entry.get("unpriced_vouchers") or 0) > 0:
            status = "amount_unavailable"
        results.append(
            {
                "model": model,
                "date": "{0}~{1}".format(start or "-", end or "-"),
                "status": status,
                "bill_tokens": row.get("total_tokens"),
                "ledger_tokens": entry.get("total_tokens"),
                "token_diff": (entry.get("total_tokens") or 0) - (row.get("total_tokens") or 0),
                "bill_amount": row.get("amount"),
                "ledger_amount": entry.get("cost_amount"),
                "amount_diff": None
                if row.get("amount") is None
                else round(float(entry.get("cost_amount") or 0) - float(row.get("amount") or 0), 6),
                "unpriced_vouchers": int(entry.get("unpriced_vouchers") or 0),
            }
        )

    ledger_only = [
        {
            "model": model,
            "date": "{0}~{1}".format(start or "-", end or "-"),
            "status": "missing_in_bill",
            "bill_tokens": None,
            "ledger_tokens": entry.get("total_tokens"),
            "token_diff": None,
            "bill_amount": None,
            "ledger_amount": entry.get("cost_amount"),
            "amount_diff": None,
        }
        for model, entry in ledger_by_model.items()
        if model not in matched_models and (models and model not in models)
    ]

    rows = results + ledger_only
    problems = [row for row in rows if row["status"] not in ("ok",)]
    return {
        "ok": not problems,
        "period": {"from": start, "to": end},
        "tolerance": {"amount": tolerance_amount, "ratio": tolerance_ratio},
        "rows": rows,
        "problems": problems,
        "ledger_models": sorted(ledger_by_model),
    }