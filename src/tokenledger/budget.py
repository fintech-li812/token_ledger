"""授权与预算控制。

内控立场：**超限的凭证照样入账，但必须被标出来**。
内控的目标是让异常可见，而不是把异常藏起来或直接丢弃。

另一个关键立场：**缺价格不等于没花钱**。
如果某个模型没有定价，其金额无法参与超限判断，此时会返回 ``indeterminate``，
而不是默默按 0 元处理。
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from .models import KIND_AUTHORIZATION, KIND_USAGE, date_str

if TYPE_CHECKING:
    from .ledger import Ledger


def scope_matches(value: str | None, pattern: str | None) -> bool:
    """授权范围支持通配符（"codex*"）。空值或 "*" 表示不限。"""
    if pattern in (None, "", "*"):
        return True
    if value is None:
        return False
    return fnmatch.fnmatchcase(str(value), str(pattern))


def create_authorization(
    ledger: "Ledger",
    *,
    agent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    limit_amount: float | None = None,
    limit_tokens: int | None = None,
    currency: str = "USD",
    period_from: str | None = None,
    period_to: str | None = None,
    approved_by: str | None = None,
    note: str | None = None,
    commit_message: str | None = None,
) -> dict:
    payload = {
        "scope": {"agent": agent, "project": project, "model": model},
        "period": {"from": period_from, "to": period_to},
        "limit": {"amount": limit_amount, "currency": currency},
        "limit_tokens": limit_tokens,
        "approved_by": approved_by,
        "note": note,
        "dedup_key": None,
    }
    return ledger.append(KIND_AUTHORIZATION, payload, commit_message=commit_message)


def applicable_authorizations(
    ledger: "Ledger",
    *,
    agent: str | None = None,
    project: str | None = None,
    model: str | None = None,
    day: str | None = None,
) -> list[dict]:
    """找出覆盖这笔支出的全部授权凭证。"""
    result = []
    for item in ledger.index.authorizations():
        payload = item["payload"]
        period = payload.get("period") or {}
        start, end = period.get("from"), period.get("to")
        if day and start and day < start:
            continue
        if day and end and day > end:
            continue
        scope = payload.get("scope") or {}
        if not scope_matches(agent, scope.get("agent")):
            continue
        if not scope_matches(project, scope.get("project")):
            continue
        if not scope_matches(model, scope.get("model")):
            continue
        entry = dict(payload)
        entry["voucher_no"] = item["voucher_no"]
        result.append(entry)
    return result


def _used_for_authorization(ledger: "Ledger", authorization: dict) -> dict:
    """授权覆盖范围内的历史用量（不含当前这笔）。

    授权里写死具体值时按该值过滤；写通配符时不做过滤（近似处理，见 docs）。
    """
    scope = authorization.get("scope") or {}
    period = authorization.get("period") or {}
    filters: dict[str, Any] = {}
    for key, column in (("agent", "agent"), ("project", "project"), ("model", "model")):
        value = scope.get(key)
        if value not in (None, "", "*") and not any(ch in str(value) for ch in "*?["):
            filters[column] = value
    return ledger.index.totals(
        kind=KIND_USAGE, start=period.get("from"), end=period.get("to"), filters=filters
    )


def _authorization_state(ledger: "Ledger", authorization: dict) -> dict:
    scope = authorization.get("scope") or {}
    period = authorization.get("period") or {}
    limit = authorization.get("limit") or {}
    limit_amount = limit.get("amount")
    limit_tokens = authorization.get("limit_tokens")
    used = _used_for_authorization(ledger, authorization)
    used_amount = float(used.get("cost_amount") or 0.0)
    used_tokens = int(used.get("total_tokens") or 0)
    unpriced = int(used.get("unpriced_vouchers") or 0)

    state = "no_limit"
    if limit_amount is not None or limit_tokens is not None:
        state = "within"
        if limit_amount is not None and used_amount >= float(limit_amount):
            state = "exceeded"
        if limit_tokens is not None and used_tokens >= int(limit_tokens):
            state = "exceeded"
        if state == "within" and unpriced > 0 and limit_amount is not None:
            state = "indeterminate"

    return {
        "status": state,
        "voucher_no": authorization.get("voucher_no"),
        "scope": scope,
        "period": period,
        "limit": limit,
        "limit_tokens": limit_tokens,
        "approved_by": authorization.get("approved_by"),
        "used_before": {
            "amount": used_amount,
            "currency": limit.get("currency") or "USD",
            "tokens": used_tokens,
            "vouchers": int(used.get("vouchers") or 0),
            "unpriced_vouchers": unpriced,
        },
    }


def evaluate(
    ledger: "Ledger",
    *,
    agent: str | None,
    project: str | None,
    model: str | None,
    day: str | None,
    cost_amount: float | None,
    total_tokens: int,
) -> dict:
    """为一笔即将入账的用量判定授权状态。"""
    authorizations = applicable_authorizations(
        ledger, agent=agent, project=project, model=model, day=day
    )
    if not authorizations:
        return {
            "status": "unbudgeted",
            "budget_ids": [],
            "detail": [],
            "note": "没有任何授权凭证覆盖这笔支出，已按例外记录",
        }

    detail = []
    for authorization in authorizations:
        state = _authorization_state(ledger, authorization)
        limit = state["limit"]
        limit_amount = limit.get("amount")
        limit_tokens = state["limit_tokens"]
        after = {
            "amount": state["used_before"]["amount"] + float(cost_amount or 0.0),
            "tokens": state["used_before"]["tokens"] + int(total_tokens or 0),
        }
        state["after"] = after

        over = state["status"] == "exceeded"
        if limit_amount is not None and after["amount"] > float(limit_amount):
            state["status"] = "exceeded"
            over = True
        if limit_tokens is not None and after["tokens"] > int(limit_tokens):
            state["status"] = "exceeded"
            over = True
        if over:
            state["exceeded_by"] = {
                "amount": round(after["amount"] - float(limit_amount), 6)
                if limit_amount is not None
                else None,
                "tokens": after["tokens"] - int(limit_tokens) if limit_tokens is not None else None,
            }
        detail.append(state)

    statuses = [item["status"] for item in detail]
    if "exceeded" in statuses:
        overall = "exceeded"
    elif "indeterminate" in statuses:
        overall = "indeterminate"
    elif "within" in statuses:
        overall = "within"
    else:
        overall = "no_limit"

    return {
        "status": overall,
        "budget_ids": [item["voucher_no"] for item in detail],
        "detail": detail,
    }


def status_report(ledger: "Ledger") -> list[dict]:
    """当前全部授权凭证的执行情况（实时重算，不看快照）。"""
    return [
        _authorization_state(ledger, dict(payload, voucher_no=item["voucher_no"]))
        for item in ledger.index.authorizations()
        for payload in [item["payload"]]
    ]