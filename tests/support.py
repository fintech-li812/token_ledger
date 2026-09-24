"""测试辅助：路径引导 + 合成日志 + 输出捕获。"""

import contextlib
import io
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
for candidate in (str(SRC_DIR), str(TESTS_DIR)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from tokenledger import Ledger  # noqa: E402
from tokenledger.cli import main  # noqa: E402

PRICING_BLOCK = """
[pricing.models."deepseek-v4-flash"]
input = 1.0
output = 2.0
cached_input = 0.1
cache_write = 1.0
"""

DEFAULT_EVENTS = [
    {"input": 1000, "cached": 0, "output": 100, "reasoning": 0, "total": 1100, "cumulative": 1100},
    {"input": 1200, "cached": 1000, "output": 200, "reasoning": 50, "total": 1400, "cumulative": 2500},
    {"input": 1300, "cached": 1200, "output": 150, "reasoning": 0, "total": 1450, "cumulative": 3950},
]

# 默认样本的期望值：单价 1.0 / 0.1 / 2.0（每百万 token）
EXPECTED_TOTAL_TOKENS = 3950
EXPECTED_COST = 0.00242


@contextlib.contextmanager
def capture():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


def init_ledger(root, *, pricing=True) -> Ledger:
    """建账本（不初始化 git，避免测试依赖外部命令），并按需写入价格表。"""
    Ledger.init(root, git=False)
    if pricing:
        config = Path(root) / "tokenledger.toml"
        config.write_text(config.read_text(encoding="utf-8") + PRICING_BLOCK, encoding="utf-8")
    return Ledger.open(root)


def write_rollout(path, *, model="deepseek-v4-flash", session_id="01a0-test-session",
                  events=None, start_ordinal=10) -> Path:
    """合成一份与真实 Codex rollout 同构的日志（含应被忽略的 response_item）。"""
    events = DEFAULT_EVENTS if events is None else events
    lines = [
        {
            "timestamp": "2026-09-24T14:00:00.000Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "id": session_id,
                "cwd": "C:\\work\\demo-project",
                "originator": "codex-tui",
                "cli_version": "0.156.1",
                "model_provider": "deepseek",
            },
        },
        {
            "timestamp": "2026-09-24T14:00:01.000Z",
            "ordinal": 1,
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "cwd": "C:\\work\\demo-project", "model": model},
        },
        {
            "timestamp": "2026-09-24T14:00:02.000Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
    ]
    ordinal = start_ordinal
    for index, event in enumerate(events, start=1):
        lines.append(
            {
                "timestamp": "2026-09-24T14:{0:02d}:00.000Z".format(index + 2),
                "ordinal": ordinal,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": event["input"],
                            "cached_input_tokens": event["cached"],
                            "cache_write_input_tokens": 0,
                            "output_tokens": event["output"],
                            "reasoning_output_tokens": event["reasoning"],
                            "total_tokens": event["cumulative"],
                        },
                        "last_token_usage": {
                            "input_tokens": event["input"],
                            "cached_input_tokens": event["cached"],
                            "cache_write_input_tokens": 0,
                            "output_tokens": event["output"],
                            "reasoning_output_tokens": event["reasoning"],
                            "total_tokens": event["total"],
                        },
                        "model_context_window": 258400,
                    },
                },
            }
        )
        ordinal += 1

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def reopen(current, root) -> Ledger:
    """换一个账本句柄：Windows 上必须先关掉上一个，否则 sqlite 文件会一直被占用。"""
    if current is not None:
        current.close()
    return Ledger.open(root)


def set_attestation_key(root, key_path) -> None:
    """把密钥路径写进账本配置。

    配置模板里已经有 [attestation] 段，所以这里替换占位行，
    而不是再追加一段（重复的 TOML 段会直接解析失败）。
    """
    config = Path(root) / "tokenledger.toml"
    text = config.read_text(encoding="utf-8")
    token = 'key_file = ""'
    if token not in text:
        raise AssertionError("配置模板里找不到 {0}".format(token))
    replacement = 'key_file = "{0}"'.format(str(key_path).replace("\\", "/"))
    config.write_text(text.replace(token, replacement, 1), encoding="utf-8")


def write_bill(path, rows) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "model,date,input_tokens,output_tokens,total_tokens,amount,currency"
    lines = [header]
    for row in rows:
        lines.append(",".join(str(row.get(key, "")) for key in
                              ("model", "date", "input_tokens", "output_tokens", "total_tokens", "amount", "currency")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path