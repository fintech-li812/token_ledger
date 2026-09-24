"""通用 JSONL 日志解析器：字段名可映射，任何 agent 都能接进来。

默认认识这些列名（不区分大小写），也可以用 ``--map field=column`` 覆盖：

    occurred_at  timestamp / time / ts / date / 日期
    agent_id     agent_id / agent / agent_name
    model        model / model_name
    session_id   session_id / session / conversation_id
    input_tokens prompt_tokens / input / input_tokens
    output_tokens completion_tokens / output / output_tokens

每行一条用量记录。去重键优先取日志里的 id / event_id，否则按
"文件内序号 + 关键数值"生成，重复采集同样不会重复入账。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..models import Usage
from .base import UsageEvent, first_match, register, to_int

DEFAULT_MAP = {
    "occurred_at": ("occurred_at", "timestamp", "time", "ts", "date", "日期"),
    "agent_id": ("agent_id", "agent", "agent_name"),
    "provider": ("provider", "model_provider"),
    "agent_version": ("agent_version", "version"),
    "model": ("model", "model_name"),
    "session_id": ("session_id", "session", "conversation_id"),
    "turn_id": ("turn_id", "turn", "request_id"),
    "project": ("project", "workspace", "cwd"),
    "input_tokens": ("input_tokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "completion_tokens", "output"),
    "cached_input_tokens": ("cached_input_tokens", "cached_tokens", "cache_read_input_tokens"),
    "cache_write_input_tokens": ("cache_write_input_tokens", "cache_creation_input_tokens"),
    "reasoning_output_tokens": ("reasoning_output_tokens", "reasoning_tokens"),
    "total_tokens": ("total_tokens", "total"),
    "dedup_key": ("dedup_key", "event_id", "id", "request_id"),
}


@register("generic")
class GenericJsonlParser:
    name = "generic"

    def parse(
        self,
        path: str | Path,
        *,
        default_model: str | None = None,
        field_map: dict | None = None,
        partial: bool = False,
        **_ignored,
    ) -> Iterator[UsageEvent]:
        overrides = field_map or {}
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue

                def pick(field: str):
                    return first_match(row, DEFAULT_MAP[field], overrides, field)

                usage = Usage(
                    input_tokens=to_int(pick("input_tokens")),
                    output_tokens=to_int(pick("output_tokens")),
                    cached_input_tokens=to_int(pick("cached_input_tokens")),
                    cache_write_input_tokens=to_int(pick("cache_write_input_tokens")),
                    reasoning_output_tokens=to_int(pick("reasoning_output_tokens")),
                    total_tokens=to_int(pick("total_tokens")),
                ).normalized()
                if not usage:
                    continue

                session_id = pick("session_id")
                raw_key = pick("dedup_key")
                dedup_key = (
                    "{0}:{1}".format(self.name, raw_key)
                    if raw_key
                    else "{0}:{1}:{2}".format(self.name, Path(path).stem, index)
                )

                yield UsageEvent(
                    occurred_at=pick("occurred_at"),
                    usage=usage,
                    dedup_key=str(dedup_key),
                    agent_id=pick("agent_id"),
                    agent_provider=pick("provider"),
                    agent_version=pick("agent_version"),
                    model=pick("model") or default_model,
                    session_id=session_id,
                    turn_id=pick("turn_id"),
                    ordinal=index,
                    cwd=pick("project"),
                    project=pick("project"),
                    partial=partial,
                )