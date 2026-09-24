"""报表渲染：表格 / JSON / CSV。表格对齐按东亚字符宽度计算，中文不会错位。"""

from __future__ import annotations

import csv
import io
import json
import unicodedata
from typing import Any, Sequence

COLUMNS_GROUPED: list[tuple[str, str]] = [
    ("grp", "分组"),
    ("vouchers", "凭证数"),
    ("input_tokens", "输入"),
    ("cached_input_tokens", "其中缓存"),
    ("output_tokens", "输出"),
    ("total_tokens", "合计 token"),
    ("cost_amount", "金额"),
    ("unpriced_vouchers", "未定价"),
    ("exceeded_vouchers", "超预算"),
]

COLUMNS_VOUCHERS: list[tuple[str, str]] = [
    ("voucher_no", "凭证号"),
    ("occurred_at", "发生时间"),
    ("agent_id", "agent"),
    ("model", "模型"),
    ("project", "项目"),
    ("session_id", "会话"),
    ("input_tokens", "输入"),
    ("output_tokens", "输出"),
    ("total_tokens", "合计"),
    ("cost_amount", "金额"),
    ("budget_status", "授权状态"),
    ("attestation_status", "来源证明"),
]


def display_width(text: Any) -> int:
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _format_cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return "{:,.4f}".format(value)
    if isinstance(value, int):
        return "{:,}".format(value)
    return str(value)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - display_width(text), 0)


def render_table(
    rows: Sequence[dict],
    columns: Sequence[tuple[str, str]],
    *,
    total_row: dict | None = None,
    empty: str = "（无数据）",
) -> str:
    if not rows and total_row is None:
        return empty
    keys = [key for key, _ in columns]
    headers = [title for _, title in columns]
    grid = [[_format_cell(row.get(key)) for key in keys] for row in rows]
    if total_row is not None:
        grid.append([_format_cell(total_row.get(key)) for key in keys])

    widths = [display_width(title) for title in headers]
    for line in grid:
        for index, cell in enumerate(line):
            widths[index] = max(widths[index], display_width(cell))

    separator = "  "
    rendered = [separator.join(_pad(title, widths[i]) for i, title in enumerate(headers))]
    rendered.append(separator.join("-" * width for width in widths))
    body = grid[:-1] if total_row is not None else grid
    rendered.extend(
        separator.join(_pad(cell, widths[i]) for i, cell in enumerate(line)) for line in body
    )
    if total_row is not None:
        rendered.append(separator.join("-" * width for width in widths))
        rendered.append(
            separator.join(_pad(cell, widths[i]) for i, cell in enumerate(grid[-1]))
        )
    return "\n".join(rendered)


def render_json(rows: Sequence[dict] | dict) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=False, default=str)


def render_csv(rows: Sequence[dict], columns: Sequence[tuple[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([title for _, title in columns])
    for row in rows:
        writer.writerow([_format_cell(row.get(key)) for key, _ in columns])
    return buffer.getvalue().rstrip("\n")


def render(rows, columns, fmt: str = "table", *, total_row: dict | None = None) -> str:
    if fmt == "json":
        return render_json(rows)
    if fmt == "csv":
        return render_csv(rows, columns)
    return render_table(rows, columns, total_row=total_row)


def totals_row(totals: dict) -> dict:
    row = dict(totals or {})
    row["grp"] = "合计"
    return row