"""日志解析器的公共接口与注册表。

解析器只做一件事：把某种 agent 日志里的用量事件，翻译成与来源无关的
:class:`UsageEvent`。入账、定价、授权判定、去重都在上层统一处理，
所以新增一个 agent 只需要写一个几十行的解析器。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..models import Usage


@dataclass
class UsageEvent:
    """一次可入账的用量事件（与具体日志格式无关）。"""

    occurred_at: str | None
    usage: Usage
    dedup_key: str
    agent_id: str | None = None
    agent_provider: str | None = None
    agent_version: str | None = None
    model: str | None = None
    session_id: str | None = None
    session_name: str | None = None
    turn_id: str | None = None
    ordinal: int | None = None
    cwd: str | None = None
    project: str | None = None
    cumulative: dict | None = None
    partial: bool = False

    def __post_init__(self) -> None:
        if self.project is None:
            self.project = path_basename(self.cwd)


def path_basename(path: str | None) -> str | None:
    """取路径末段，同时兼容 Windows 与 POSIX 分隔符（跨平台解析日志时必须）。"""
    if not path:
        return None
    cleaned = str(path).replace("\\", "/").rstrip("/")
    name = cleaned.rsplit("/", 1)[-1]
    return name or None


def iter_source_files(target: str | Path, pattern: str = "*.jsonl") -> list[Path]:
    path = Path(target).expanduser()
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and fnmatch.fnmatch(candidate.name, pattern)
        )
    return []


_REGISTRY: dict[str, type] = {}


def register(name: str):
    def decorator(cls: type) -> type:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def available_parsers() -> list[str]:
    return sorted(_REGISTRY)


def get_parser(name: str) -> type:
    if name not in _REGISTRY:
        raise KeyError("未知的日志解析器 {0}；可用：{1}".format(name, ", ".join(available_parsers())))
    return _REGISTRY[name]


def parse_kv_map(pairs: list[str] | None) -> dict:
    """把 --map field=column 解析成字典。"""
    mapping = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError("--map 需要 field=column 形式，收到：{0}".format(pair))
        key, value = pair.split("=", 1)
        mapping[key.strip()] = value.strip()
    return mapping


def first_match(row: dict, candidates: tuple[str, ...], overrides: dict | None = None,
                field: str | None = None):
    """按候选列名（不区分大小写）取值；overrides 里的显式映射优先。"""
    if overrides and field and field in overrides:
        column = overrides[field]
        for key, value in row.items():
            if str(key).strip().lower() == column.lower():
                return value
        return None
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def to_int(value) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


_SPLIT_RE = re.compile(r"[\\/]")


def safe_name(path: str | None) -> str | None:
    return _SPLIT_RE.split(str(path))[-1] if path else None