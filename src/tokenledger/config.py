"""配置加载：可移植优先，任何路径都不硬编码在代码里。

优先级（后者覆盖前者）：
    1. 代码内置默认值
    2. 用户级配置   $TOKENLEDGER_HOME/tokenledger.toml
                    （Windows 默认 %APPDATA%\tokenledger\tokenledger.toml，
                     其他平台默认 $XDG_CONFIG_HOME/tokenledger/tokenledger.toml）
    3. 账本级配置   <账本目录>/tokenledger.toml
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "tokenledger.toml"
ENV_REPO = "TOKENLEDGER_REPO"
ENV_HOME = "TOKENLEDGER_HOME"

DEFAULTS: dict[str, Any] = {
    "ledger": {"dir": "ledger", "auto_commit": True},
    "report": {"default_group_by": "agent", "top": 20},
    "pricing": {"currency": "USD", "cached_input_included_in_input": True, "models": {}},
    "ingest": {"codex": {"home": "", "default_model": ""}, "generic": {}},
}

CONFIG_TEMPLATE = """# tokenledger 账本配置
# 用户级配置放在 $TOKENLEDGER_HOME/tokenledger.toml，本文件会覆盖它。

[ledger]
dir = "ledger"          # 凭证数据目录（相对账本根目录）
auto_commit = true      # 入账后自动 git 提交，形成第二重留痕

[report]
default_group_by = "agent"
top = 20

[pricing]
currency = "USD"
# 记账约定：input_tokens 是否已经包含 cached_input_tokens（Codex / OpenAI 为 true）
cached_input_included_in_input = true

# 单价单位：每百万 token。默认留空 —— 未定价的凭证会被标记为 unpriced，
# 并在 `tledger report --exceptions` 中列出，绝不会被当成 0 元。
# [pricing.models."deepseek-v4-flash"]
# input = 0.0
# output = 0.0
# cached_input = 0.0
# cache_write = 0.0

[ingest.codex]
# 留空则自动探测：$CODEX_HOME/sessions，其次 ~/.codex/sessions
home = ""
default_model = ""
"""


class ConfigError(RuntimeError):
    """配置读取或解析失败。"""


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("无法读取配置文件 {0}: {1}".format(path, exc)) from exc


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def user_config_path() -> Path:
    home = os.environ.get(ENV_HOME)
    if home:
        return Path(home).expanduser() / CONFIG_FILENAME
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "tokenledger" / CONFIG_FILENAME


def codex_home() -> Path:
    """定位 Codex 数据目录：$CODEX_HOME 优先，否则 ~/.codex。"""
    explicit = os.environ.get("CODEX_HOME")
    return Path(explicit).expanduser() if explicit else Path.home() / ".codex"


def find_repo_root(start: str | Path | None = None) -> Path | None:
    """从 start 起向上找账本根目录（含 tokenledger.toml 的目录）。"""
    env = os.environ.get(ENV_REPO)
    if env:
        return Path(env).expanduser().resolve()
    current = Path(start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


class Config:
    def __init__(self, root: str | Path, data: dict):
        self.root = Path(root).expanduser().resolve()
        self.data = data

    @classmethod
    def load(cls, root: str | Path) -> "Config":
        data = dict(DEFAULTS)
        user_file = user_config_path()
        if user_file.is_file():
            data = _merge(data, _read_toml(user_file))
        repo_file = Path(root).expanduser() / CONFIG_FILENAME
        if repo_file.is_file():
            data = _merge(data, _read_toml(repo_file))
        return cls(root, data)

    # -- 取值 ------------------------------------------------------------- #
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def ledger_dir(self) -> Path:
        return self.root / str(self.get("ledger.dir", "ledger"))

    @property
    def auto_commit(self) -> bool:
        return bool(self.get("ledger.auto_commit", True))

    @property
    def currency(self) -> str:
        return str(self.get("pricing.currency") or "USD")

    @property
    def pricing_models(self) -> dict:
        return self.get("pricing.models", {}) or {}

    @property
    def cached_input_included(self) -> bool:
        return bool(self.get("pricing.cached_input_included_in_input", True))

    @property
    def codex_sessions_dir(self) -> Path:
        configured = str(self.get("ingest.codex.home", "") or "")
        base = Path(configured).expanduser() if configured else codex_home()
        return base / "sessions"