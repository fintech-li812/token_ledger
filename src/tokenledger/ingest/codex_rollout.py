"""解析 Codex CLI 的 rollout 日志（默认位置 $CODEX_HOME/sessions/**/rollout-*.jsonl）。

已在真实日志上核对过的结构：

* ``type=session_meta``：``session_id`` / ``cwd`` / ``originator`` / ``cli_version`` / ``model_provider``
* ``type=turn_context``：``turn_id`` / ``cwd`` / 模型名（payload.model）
* ``type=event_msg`` 且 ``payload.type=token_count``：

  - ``payload.info.last_token_usage``  —— 本回合**增量**，就是本张凭证
  - ``payload.info.total_token_usage`` —— 会话**累计**，是"总账"，用于账实核对

一条 token_count 事件 = 一张凭证，去重键为 ``codex:<session_id>:<ordinal>``，
因此同一天重复采集不会重复入账。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..models import Usage
from .base import UsageEvent, register, to_int


@register("codex")
class CodexRolloutParser:
    name = "codex"

    def parse(
        self,
        path: str | Path,
        *,
        default_model: str | None = None,
        session_names: dict | None = None,
        partial: bool = False,
        **_ignored,
    ) -> Iterator[UsageEvent]:
        session_names = session_names or {}
        session_id = None
        turn_id = None
        cwd = None
        originator = None
        provider = None
        cli_version = None
        model = default_model

        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                kind = record.get("type")
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if kind == "session_meta":
                    session_id = payload.get("session_id") or payload.get("id") or session_id
                    cwd = payload.get("cwd") or cwd
                    originator = payload.get("originator") or originator
                    provider = payload.get("model_provider") or provider
                    cli_version = payload.get("cli_version") or cli_version
                    model = payload.get("model") or model
                    continue

                if kind == "turn_context":
                    turn_id = payload.get("turn_id") or turn_id
                    cwd = payload.get("cwd") or cwd
                    model = payload.get("model") or model
                    continue

                if kind != "event_msg":
                    continue

                event_type = payload.get("type")
                if event_type == "task_started":
                    turn_id = payload.get("turn_id") or turn_id
                    continue
                if event_type != "token_count":
                    continue

                info = payload.get("info") or {}
                delta = info.get("last_token_usage") or {}
                cumulative = info.get("total_token_usage") or {}
                usage = Usage.from_dict(delta).normalized()
                if not usage:
                    continue

                ordinal = record.get("ordinal")
                yield UsageEvent(
                    occurred_at=record.get("timestamp"),
                    usage=usage,
                    dedup_key="codex:{0}:{1}".format(session_id or Path(path).stem, ordinal),
                    agent_id=originator,
                    agent_provider=provider,
                    agent_version=cli_version,
                    model=model,
                    session_id=session_id,
                    session_name=session_names.get(session_id),
                    turn_id=turn_id,
                    ordinal=ordinal,
                    cwd=cwd,
                    partial=partial,
                    cumulative={"total_tokens": to_int(cumulative.get("total_tokens"))}
                    if cumulative
                    else None,
                )