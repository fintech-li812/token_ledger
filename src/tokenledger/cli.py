"""命令行入口 tledger。

任何路径都不写死：账本位置来自 ``--repo``、``$TOKENLEDGER_REPO``，或从当前目录向上查找
``tokenledger.toml``。因此同一份代码在 Windows / macOS / Linux 上行为一致。

退出码：
    0  正常
    1  参数或 IO 错误
    3  审计不通过（verify）
    4  对账差异超容差（reconcile）
    5  预算超限（--strict 时）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import repo as gitrepo
from .budget import create_authorization, evaluate, status_report
from .config import ENV_REPO, ConfigError, find_repo_root
from .ingest import available_parsers, get_parser, iter_source_files, parse_kv_map
from .ledger import DuplicateVoucher, Ledger, LedgerError, build_usage_payload
from .models import (
    KIND_AUTHORIZATION,
    KIND_USAGE,
    Usage,
    canonical_json,
    date_str,
    sha256_file,
)
from .pricing import price_usage
from .reconcile import ReconcileError, compare, read_bill
from .report import COLUMNS_GROUPED, COLUMNS_VOUCHERS, render, render_csv, render_json, render_table
from .store import GROUP_EXPRESSIONS

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUDIT_FAILED = 3
EXIT_RECONCILE_DIFF = 4
EXIT_BUDGET_EXCEEDED = 5

BUDGET_STATUS_TEXT = {
    "within": "未超限",
    "exceeded": "已超限",
    "indeterminate": "无法判定（有未定价用量）",
    "no_limit": "未设额度",
    "unbudgeted": "无授权",
}

RECONCILE_STATUS_TEXT = {
    "ok": "一致",
    "diff": "有差异",
    "missing_in_ledger": "账本缺失",
    "missing_in_bill": "账单缺失",
    "amount_unavailable": "金额不可得",
}

EXCEPTION_COLUMNS = [
    ("voucher_no", "凭证号"),
    ("occurred_at", "发生时间"),
    ("agent_id", "agent"),
    ("model", "模型"),
    ("total_tokens", "合计 token"),
    ("cost_amount", "金额"),
    ("budget_status", "授权状态"),
    ("cost_status", "定价状态"),
]

AUTHORIZATION_COLUMNS = [
    ("voucher_no", "授权凭证号"),
    ("agent_id", "agent"),
    ("project", "项目"),
    ("model", "模型"),
    ("occurred_at", "起始"),
    ("cost_amount", "额度"),
    ("cost_currency", "币种"),
]

BUDGET_STATUS_COLUMNS = [
    ("voucher_no", "授权凭证号"),
    ("agent", "agent"),
    ("project", "项目"),
    ("period", "期间"),
    ("limit", "额度"),
    ("used_amount", "已用金额"),
    ("limit_tokens", "token 额度"),
    ("used_tokens", "已用 token"),
    ("unpriced_vouchers", "未定价"),
    ("status", "状态"),
    ("approved_by", "审批人"),
]

RECONCILE_COLUMNS = [
    ("model", "模型"),
    ("date", "期间"),
    ("status", "结论"),
    ("bill_tokens", "账单 token"),
    ("ledger_tokens", "账本 token"),
    ("token_diff", "token 差"),
    ("bill_amount", "账单金额"),
    ("ledger_amount", "账本金额"),
    ("amount_diff", "金额差"),
]

EXPORT_COLUMNS = (
    "voucher_no", "kind", "recorded_at", "occurred_at", "agent_id", "agent_provider",
    "model", "project", "session_id", "turn_id", "input_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens",
    "cost_amount", "cost_currency", "cost_status", "budget_status", "budget_ids",
    "source_parser", "source_file",
)


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
_OPEN_LEDGERS: list = []


def _open_ledger(args) -> Ledger:
    if getattr(args, "repo", None):
        root = Path(args.repo).expanduser()
    else:
        root = find_repo_root()
        if root is None:
            raise LedgerError(
                "没找到账本目录：用 --repo 指定，或在账本目录下执行，或设置环境变量 " + ENV_REPO
            )
    ledger = Ledger.open(root)
    # 记下来统一关闭：Windows 上未关闭的 sqlite 连接会占住文件
    _OPEN_LEDGERS.append(ledger)
    return ledger


def _close_ledgers() -> None:
    while _OPEN_LEDGERS:
        _OPEN_LEDGERS.pop().close()


def _load_session_names(home: Path) -> dict:
    """读取 Codex 的 session_index.jsonl，把会话 ID 映射成可读的线程名。"""
    index_file = home / "session_index.jsonl"
    if not index_file.is_file():
        return {}
    names = {}
    try:
        with open(index_file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    continue
                identifier = row.get("id") or row.get("session_id")
                name = row.get("thread_name") or row.get("name")
                if identifier and name:
                    names[identifier] = name
    except OSError:
        return {}
    return names


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    if args.path:
        target = Path(args.path).expanduser()
    else:
        target = find_repo_root() or Path.cwd()
    result = Ledger.init(target, git=not args.no_git, force=args.force)
    print("账本目录：{0}".format(result["root"]))
    if result["config_written"]:
        print("配置已生成：{0}".format(result["config"]))
    else:
        print("配置已存在，未覆盖：{0}（需要重写请加 --force）".format(result["config"]))
    for kind, path in result["streams"].items():
        print("凭证流水（{0}）：{1}".format(kind, path))
    git_info = result.get("git")
    if git_info is not None:
        if git_info.get("ok"):
            print("git 仓库：就绪")
        else:
            print("git 仓库：{0}".format(git_info.get("reason") or git_info.get("stderr") or "未就绪"))
    commit = result.get("commit")
    if commit is not None:
        print("初始提交：{0}".format("已完成" if commit.get("committed") else commit.get("reason")))
    print("")
    print("下一步：")
    print("  tledger --repo \"{0}\" budget set --agent <agent> --limit-usd 50".format(result["root"]))
    print("  tledger --repo \"{0}\" ingest codex --since <YYYY-MM-DD>".format(result["root"]))
    return EXIT_OK


def cmd_record(args) -> int:
    ledger = _open_ledger(args)
    usage = Usage(
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cached_input_tokens=args.cached_input_tokens,
        cache_write_input_tokens=args.cache_write_input_tokens,
        reasoning_output_tokens=args.reasoning_output_tokens,
        total_tokens=args.total_tokens,
    ).normalized()
    if not usage:
        print("录入失败：至少需要一个非零的 token 数", file=sys.stderr)
        return EXIT_ERROR

    cost = price_usage(
        usage,
        args.model,
        ledger.config.pricing_models,
        currency=ledger.config.currency,
        cached_input_included=ledger.config.cached_input_included,
    )
    budget = evaluate(
        ledger,
        agent=args.agent,
        project=args.project,
        model=args.model,
        day=date_str(args.occurred_at),
        cost_amount=cost.get("amount"),
        total_tokens=usage.total_tokens,
    )
    payload = build_usage_payload(
        agent_id=args.agent,
        model=args.model,
        usage=usage,
        occurred_at=args.occurred_at,
        provider=args.provider,
        agent_version=args.agent_version,
        session={"id": args.session, "turn_id": args.turn},
        project=args.project,
        source={"parser": "manual"},
        cost=cost,
        budget={"status": budget["status"], "budget_ids": budget["budget_ids"]},
        dedup_key=args.dedup_key,
        recorded_by=args.recorded_by or "tledger/{0} record".format(__version__),
        approved_by=args.approved_by,
    )
    record = ledger.append(
        KIND_USAGE,
        payload,
        dedup_key=args.dedup_key,
        commit_message="feat(ledger): record usage voucher",
    )
    print(
        "已入账：{0}   金额：{1} {2}   授权状态：{3}".format(
            record["voucher_no"],
            cost.get("amount") if cost.get("amount") is not None else "未定价",
            cost.get("currency"),
            BUDGET_STATUS_TEXT.get(budget["status"], budget["status"]),
        )
    )
    if args.strict and budget["status"] == "exceeded":
        return EXIT_BUDGET_EXCEEDED
    return EXIT_OK


def cmd_ingest(args) -> int:
    ledger = _open_ledger(args)
    try:
        parser_cls = get_parser(args.parser)
    except KeyError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    parser = parser_cls()
    try:
        field_map = parse_kv_map(args.map)
    except ValueError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.path:
        sources = [Path(item).expanduser() for item in args.path]
    elif args.parser == "codex":
        sources = [ledger.config.codex_sessions_dir]
    else:
        print("错误：{0} 解析器需要 --path 指定日志文件或目录".format(args.parser), file=sys.stderr)
        return EXIT_ERROR

    files: list[Path] = []
    for source in sources:
        if not source.exists():
            print("跳过不存在的路径：{0}".format(source), file=sys.stderr)
            continue
        files.extend(iter_source_files(source, pattern=args.glob))
    if not files:
        print("没有找到可解析的日志文件（glob={0}）".format(args.glob), file=sys.stderr)
        return EXIT_ERROR

    session_names = {}
    if args.parser == "codex":
        session_names = _load_session_names(ledger.config.codex_sessions_dir.parent)

    planned = []
    for path in files:
        try:
            events = list(
                parser.parse(
                    path,
                    default_model=args.default_model,
                    session_names=session_names,
                    field_map=field_map,
                    partial=False,
                )
            )
        except OSError as exc:
            # 正在被 agent 写入的活跃日志可能被独占占用，跳过而不是中断整批采集
            print("跳过无法读取的日志：{0}（{1}）".format(path, exc), file=sys.stderr)
            continue
        total_by_session: dict = {}
        for event in events:
            total_by_session[event.session_id] = total_by_session.get(event.session_id, 0) + 1
        digest = sha256_file(path)
        for event in events:
            day = date_str(event.occurred_at)
            if args.since and day and day < args.since:
                continue
            if args.until and day and day > args.until:
                continue
            planned.append((path, digest, event, total_by_session.get(event.session_id, 1)))

    planned.sort(key=lambda item: (str(item[2].occurred_at or ""), item[2].ordinal or 0))
    if args.limit:
        planned = planned[: args.limit]

    # 只采到会话的一部分时，标记 partial —— 这样的会话无法做"明细 vs 累计"核对
    kept_by_session: dict = {}
    for _path, _digest, event, _total in planned:
        kept_by_session[event.session_id] = kept_by_session.get(event.session_id, 0) + 1
    for _path, _digest, event, total in planned:
        if event.session_id and kept_by_session.get(event.session_id, 0) < total:
            event.partial = True

    existing = ledger.dedup_keys(KIND_USAGE)
    pricing_models = ledger.config.pricing_models
    currency = ledger.config.currency
    appended = skipped = exceeded = unpriced = 0

    for path, digest, event, _total in planned:
        if event.dedup_key in existing:
            skipped += 1
            continue
        usage = event.usage.normalized()
        cost = price_usage(
            usage,
            event.model,
            pricing_models,
            currency=currency,
            cached_input_included=ledger.config.cached_input_included,
        )
        if cost.get("status") == "unpriced":
            unpriced += 1
        budget = evaluate(
            ledger,
            agent=event.agent_id,
            project=event.project,
            model=event.model,
            day=date_str(event.occurred_at),
            cost_amount=cost.get("amount"),
            total_tokens=usage.total_tokens,
        )
        if budget["status"] == "exceeded":
            exceeded += 1

        payload = build_usage_payload(
            agent_id=event.agent_id,
            model=event.model,
            usage=usage,
            occurred_at=event.occurred_at,
            provider=event.agent_provider,
            agent_version=event.agent_version,
            session={
                "id": event.session_id,
                "name": event.session_name,
                "turn_id": event.turn_id,
                "ordinal": event.ordinal,
                "cwd": event.cwd,
                "partial": True if event.partial else None,
            },
            project=event.project,
            source={"parser": parser_cls.name, "file": str(path), "sha256": digest},
            cost=cost,
            budget={"status": budget["status"], "budget_ids": budget["budget_ids"]},
            cumulative=event.cumulative,
            dedup_key=event.dedup_key,
            recorded_by="tledger/{0} ingest:{1}".format(__version__, parser_cls.name),
        )

        if args.dry_run:
            print(canonical_json(payload))
            continue
        ledger.append(KIND_USAGE, payload, dedup_key=event.dedup_key, existing_keys=existing)
        existing.add(event.dedup_key)
        appended += 1

    commit_note = "未提交"
    if appended and not args.dry_run and not args.no_commit and ledger.config.auto_commit:
        commit = gitrepo.commit_all(
            ledger.root,
            "feat(ledger): ingest {0} voucher(s) via {1}".format(appended, parser_cls.name),
        )
        commit_note = "已提交" if commit.get("committed") else commit.get("reason") or "未提交"

    print(
        "采集完成：源文件 {0} 个，新增凭证 {1} 张，跳过 {2} 条（已存在）".format(
            len(files), appended, skipped
        )
    )
    print("其中未定价 {0} 条（金额不可得），超预算 {1} 条".format(unpriced, exceeded))
    print("git：{0}".format(commit_note))
    if exceeded and args.strict:
        return EXIT_BUDGET_EXCEEDED
    return EXIT_OK


def cmd_query(args) -> int:
    ledger = _open_ledger(args)
    filters = {
        "agent": args.agent,
        "model": args.model,
        "project": args.project,
        "session": args.session,
        "budget_status": args.budget_status,
        "cost_status": args.cost_status,
    }
    rows = ledger.index.query(
        kind=KIND_USAGE, start=args.since, end=args.until, limit=args.limit, filters=filters
    )
    if args.format == "json":
        print(render_json(rows))
    else:
        print(render(rows, COLUMNS_VOUCHERS, args.format))
    return EXIT_OK


def _exceptions_report(ledger: Ledger, args, base_filters: dict) -> int:
    groups = (
        ("超预算凭证", {"budget_status": "exceeded"}),
        ("无授权凭证", {"budget_status": "unbudgeted"}),
        ("未定价凭证", {"cost_status": "unpriced"}),
    )
    found = 0
    for title, extra in groups:
        filters = dict(base_filters)
        filters.update(extra)
        totals = ledger.index.totals(
            kind=KIND_USAGE, start=args.since, end=args.until, filters=filters
        )
        count = int(totals.get("vouchers") or 0)
        found += count
        print("【{0}】{1} 条".format(title, count))
        if count:
            rows = ledger.index.query(
                kind=KIND_USAGE,
                start=args.since,
                end=args.until,
                filters=filters,
                limit=args.limit or 20,
            )
            print(render_table(rows, EXCEPTION_COLUMNS))
        print("")
    if not found:
        print("没有发现例外凭证：授权、定价、账本三方面都干净。")
    return EXIT_OK


def cmd_report(args) -> int:
    ledger = _open_ledger(args)
    filters = {"agent": args.agent, "model": args.model, "project": args.project}
    if args.exceptions:
        return _exceptions_report(ledger, args, filters)

    group_by = args.group_by or ledger.config.get("report.default_group_by", "agent")
    rows = ledger.index.aggregate(
        group_by=group_by, kind=KIND_USAGE, start=args.since, end=args.until, filters=filters
    )
    totals = ledger.index.totals(
        kind=KIND_USAGE, start=args.since, end=args.until, filters=filters
    )
    if args.format == "json":
        print(
            render_json(
                {
                    "group_by": group_by,
                    "period": {"from": args.since, "to": args.until},
                    "totals": totals,
                    "rows": rows,
                }
            )
        )
        return EXIT_OK

    total_row = {"grp": "合计"}
    for key in (
        "vouchers",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_amount",
        "unpriced_vouchers",
        "exceeded_vouchers",
    ):
        total_row[key] = totals.get(key)
    print("按 {0} 汇总".format(group_by))
    print(render(rows, COLUMNS_GROUPED, args.format, total_row=total_row))
    if int(totals.get("unpriced_vouchers") or 0):
        print("")
        print(
            "提示：{0} 张凭证未定价，金额合计不完整（tledger report --exceptions 查看明细）".format(
                totals["unpriced_vouchers"]
            )
        )
    return EXIT_OK


def cmd_budget(args) -> int:
    ledger = _open_ledger(args)
    if args.action == "set":
        if args.limit_usd is None and args.limit_tokens is None:
            print("错误：至少给出 --limit-usd 或 --limit-tokens", file=sys.stderr)
            return EXIT_ERROR
        record = create_authorization(
            ledger,
            agent=args.agent,
            project=args.project,
            model=args.model,
            limit_amount=args.limit_usd,
            limit_tokens=args.limit_tokens,
            currency=args.currency or ledger.config.currency,
            period_from=args.period_from,
            period_to=args.period_to,
            approved_by=args.approved_by,
            note=args.note,
        )
        if ledger.config.auto_commit:
            gitrepo.commit_all(
                ledger.root, "feat(ledger): authorize {0}".format(record["voucher_no"])
            )
        print("已签发授权凭证：{0}".format(record["voucher_no"]))
        return EXIT_OK

    if args.action == "list":
        rows = ledger.index.query(kind=KIND_AUTHORIZATION, order="voucher_no ASC")
        print(render_table(rows, AUTHORIZATION_COLUMNS))
        return EXIT_OK

    report = status_report(ledger)
    rows = []
    for item in report:
        scope = item["scope"] or {}
        period = item["period"] or {}
        rows.append(
            {
                "voucher_no": item["voucher_no"],
                "agent": scope.get("agent") or "*",
                "project": scope.get("project") or "*",
                "period": "{0} ~ {1}".format(period.get("from") or "-", period.get("to") or "-"),
                "limit": (item["limit"] or {}).get("amount"),
                "used_amount": item["used_before"]["amount"],
                "limit_tokens": item["limit_tokens"],
                "used_tokens": item["used_before"]["tokens"],
                "unpriced_vouchers": item["used_before"]["unpriced_vouchers"],
                "status": BUDGET_STATUS_TEXT.get(item["status"], item["status"]),
                "approved_by": item["approved_by"] or "-",
            }
        )
    if not rows:
        print("还没有任何授权凭证，先执行：tledger budget set --agent <agent> --limit-usd 50")
        return EXIT_OK
    print(render_table(rows, BUDGET_STATUS_COLUMNS))
    exceeded = [item for item in report if item["status"] == "exceeded"]
    if exceeded:
        print("")
        print("注意：{0} 项授权已超限。超限凭证已照常入账并标记，请复核。".format(len(exceeded)))
        if args.strict:
            return EXIT_BUDGET_EXCEEDED
    return EXIT_OK


def cmd_reconcile(args) -> int:
    ledger = _open_ledger(args)
    try:
        bill_rows = read_bill(args.bill)
    except ReconcileError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    result = compare(
        ledger,
        bill_rows,
        tolerance_amount=args.tolerance_amount,
        tolerance_ratio=args.tolerance_ratio,
    )
    if args.format == "json":
        print(render_json(result))
    else:
        rows = [
            dict(row, status=RECONCILE_STATUS_TEXT.get(row["status"], row["status"]))
            for row in result["rows"]
        ]
        print(render_table(rows, RECONCILE_COLUMNS))
        print("")
        print(
            "对账期间：{0} ~ {1}".format(
                result["period"]["from"] or "-", result["period"]["to"] or "-"
            )
        )
        print(
            "容差：金额 {0}，比例 {1}".format(
                result["tolerance"]["amount"], result["tolerance"]["ratio"]
            )
        )
        if result["ok"]:
            print("结论：账单与账本完全一致。")
        else:
            print("结论：存在 {0} 项差异，逐项复核后再确认。".format(len(result["problems"])))
    return EXIT_OK if result["ok"] else EXIT_RECONCILE_DIFF


def cmd_verify(args) -> int:
    ledger = _open_ledger(args)
    result = ledger.verify()
    if args.format == "json":
        print(render_json(result))
        return EXIT_OK if result["ok"] else EXIT_AUDIT_FAILED

    print("审计结论：{0}".format("通过" if result["ok"] else "不通过"))
    for kind, info in result["kinds"].items():
        print(
            "  {0}：{1} 张凭证，链头 {2}".format(
                kind, info["vouchers"], (info["head_hash"] or "")[:16] or "-"
            )
        )
    index_info = result["index"]
    print(
        "  索引：明细账 {0} 条 / 索引 {1} 条".format(
            index_info["jsonl_vouchers"], index_info["index_vouchers"]
        )
    )
    for entry in result["session_totals"]:
        if entry["status"] != "matched":
            print(
                "  会话 {0}：{1}（明细 {2} / 日志累计 {3}）".format(
                    entry["session_id"],
                    entry["status"],
                    entry["delta_total_tokens"],
                    entry["cumulative_total_tokens"],
                )
            )
    if result["errors"]:
        print("")
        for error in result["errors"]:
            print("  ✗ {0}".format(error))
        print("")
        print("建议：先 tledger reindex 重建索引再复验；若明细账本身被改过，则需人工追溯。")
    return EXIT_OK if result["ok"] else EXIT_AUDIT_FAILED


def cmd_reindex(args) -> int:
    ledger = _open_ledger(args)
    result = ledger.reindex()
    counts = result["vouchers"]
    print(
        "索引已重建：用量凭证 {0} 张，授权凭证 {1} 张，合计 {2} 张".format(
            counts.get(KIND_USAGE, 0), counts.get(KIND_AUTHORIZATION, 0), counts.get("total", 0)
        )
    )
    return EXIT_OK


def cmd_export(args) -> int:
    ledger = _open_ledger(args)
    kinds = [args.kind] if args.kind else [KIND_USAGE, KIND_AUTHORIZATION]
    if args.format == "jsonl":
        lines = [
            canonical_json(record) for kind in kinds for record in ledger.read_records(kind)
        ]
        text = "\n".join(lines)
    else:
        rows = [
            row
            for kind in kinds
            for row in ledger.index.query(kind=kind, order="occurred_at ASC", columns=EXPORT_COLUMNS)
        ]
        text = render_csv(rows, [(column, column) for column in EXPORT_COLUMNS])
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
        print("已导出到 {0}".format(args.out))
    else:
        print(text)
    return EXIT_OK


def cmd_history(args) -> int:
    ledger = _open_ledger(args)
    lines = gitrepo.log(ledger.root, limit=args.limit)
    if not lines:
        print("没有 git 历史（可能未初始化仓库，或还没有提交）")
        return EXIT_OK
    print("\n".join(lines))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 参数定义
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo",
        # 子解析器里同名参数若带默认值，会用它覆盖父解析器已解析的值；SUPPRESS 可避免
        default=argparse.SUPPRESS,
        help="账本目录（默认从当前目录向上查找 tokenledger.toml，或读 $" + ENV_REPO + "）",
    )

    parser = argparse.ArgumentParser(
        prog="tledger",
        parents=[common],
        description="审计级 AI token 用量账本：凭证化、授权、对账、审计留痕",
    )
    parser.add_argument(
        "--version", action="version", version="tokenledger {0}".format(__version__)
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common], help="建账本（含本地 git 仓库）")
    p_init.add_argument("path", nargs="?", help="账本目录，默认当前目录")
    p_init.add_argument("--no-git", action="store_true", help="不初始化 git 仓库")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")
    p_init.set_defaults(handler=cmd_init)

    p_record = sub.add_parser("record", parents=[common], help="手工补录一笔用量凭证")
    p_record.add_argument("--agent", required=True)
    p_record.add_argument("--model", required=True)
    p_record.add_argument("--provider")
    p_record.add_argument("--agent-version")
    p_record.add_argument("--input-tokens", type=int, default=0)
    p_record.add_argument("--output-tokens", type=int, default=0)
    p_record.add_argument("--cached-input-tokens", type=int, default=0)
    p_record.add_argument("--cache-write-tokens", type=int, default=0)
    p_record.add_argument("--reasoning-output-tokens", type=int, default=0)
    p_record.add_argument("--total-tokens", type=int, default=0)
    p_record.add_argument("--occurred-at", help="发生时间，默认现在（UTC）")
    p_record.add_argument("--session")
    p_record.add_argument("--turn")
    p_record.add_argument("--project")
    p_record.add_argument("--dedup-key", help="幂等键：重复入账会被拒绝")
    p_record.add_argument("--recorded-by")
    p_record.add_argument("--approved-by")
    p_record.add_argument("--strict", action="store_true", help="超预算时返回退出码 5")
    p_record.set_defaults(handler=cmd_record)

    p_ingest = sub.add_parser("ingest", parents=[common], help="解析日志批量入账")
    p_ingest.add_argument("parser", choices=available_parsers())
    p_ingest.add_argument("--path", action="append", help="日志文件或目录，可重复")
    p_ingest.add_argument("--glob", default="*.jsonl", help="目录内的文件名匹配，默认 *.jsonl")
    p_ingest.add_argument("--since", help="只采集该日期（含）之后，格式 YYYY-MM-DD（UTC 日）")
    p_ingest.add_argument("--until", help="只采集该日期（含）之前")
    p_ingest.add_argument("--limit", type=int, help="最多入账多少条（按时间升序截取）")
    p_ingest.add_argument("--default-model", help="日志里没有模型名时的兜底值")
    p_ingest.add_argument("--map", action="append", default=[], help="字段映射 field=column，可重复")
    p_ingest.add_argument("--dry-run", action="store_true", help="只预览，不写账")
    p_ingest.add_argument("--no-commit", action="store_true", help="本次不自动 git 提交")
    p_ingest.add_argument("--strict", action="store_true", help="有超预算凭证时返回退出码 5")
    p_ingest.set_defaults(handler=cmd_ingest)

    p_query = sub.add_parser("query", parents=[common], help="凭证级明细查询")
    p_query.add_argument("--agent")
    p_query.add_argument("--model")
    p_query.add_argument("--project")
    p_query.add_argument("--session")
    p_query.add_argument("--budget-status", choices=["within", "exceeded", "unbudgeted", "indeterminate", "no_limit"])
    p_query.add_argument("--cost-status", choices=["priced", "unpriced"])
    p_query.add_argument("--since")
    p_query.add_argument("--until")
    p_query.add_argument("--limit", type=int, default=50)
    p_query.add_argument("--format", choices=["table", "json", "csv"], default="table")
    p_query.set_defaults(handler=cmd_query)

    p_report = sub.add_parser("report", parents=[common], help="汇总报表 / 例外报告")
    p_report.add_argument("--group-by", choices=sorted(GROUP_EXPRESSIONS), help="汇总维度")
    p_report.add_argument("--agent")
    p_report.add_argument("--model")
    p_report.add_argument("--project")
    p_report.add_argument("--since")
    p_report.add_argument("--until")
    p_report.add_argument("--limit", type=int)
    p_report.add_argument("--exceptions", action="store_true", help="只列例外凭证")
    p_report.add_argument("--format", choices=["table", "json", "csv"], default="table")
    p_report.set_defaults(handler=cmd_report)

    p_budget = sub.add_parser("budget", parents=[common], help="授权与预算")
    budget_sub = p_budget.add_subparsers(dest="action", required=True)

    p_bset = budget_sub.add_parser("set", parents=[common], help="签发授权凭证")
    p_bset.add_argument("--agent")
    p_bset.add_argument("--project")
    p_bset.add_argument("--model")
    p_bset.add_argument("--limit-usd", type=float, dest="limit_usd", help="金额上限")
    p_bset.add_argument("--limit-tokens", type=int, help="token 上限")
    p_bset.add_argument("--currency")
    p_bset.add_argument("--from", dest="period_from", help="生效日期 YYYY-MM-DD")
    p_bset.add_argument("--to", dest="period_to", help="失效日期 YYYY-MM-DD")
    p_bset.add_argument("--approved-by", help="审批人（职务分离留痕）")
    p_bset.add_argument("--note")
    p_bset.set_defaults(handler=cmd_budget)

    p_blist = budget_sub.add_parser("list", parents=[common], help="列授权凭证")
    p_blist.set_defaults(handler=cmd_budget)

    p_bstatus = budget_sub.add_parser("status", parents=[common], help="授权执行情况")
    p_bstatus.add_argument("--strict", action="store_true", help="超限时返回退出码 5")
    p_bstatus.set_defaults(handler=cmd_budget)

    p_rec = sub.add_parser("reconcile", parents=[common], help="与供应商账单对账")
    p_rec.add_argument("--bill", required=True, help="账单 CSV")
    p_rec.add_argument("--tolerance-amount", type=float, default=0.0, help="金额容差")
    p_rec.add_argument("--tolerance-ratio", type=float, default=0.0, help="比例容差，如 0.01")
    p_rec.add_argument("--format", choices=["table", "json"], default="table")
    p_rec.set_defaults(handler=cmd_reconcile)

    p_verify = sub.add_parser("verify", parents=[common], help="审计：哈希链 / 缺号 / 索引 / 账实")
    p_verify.add_argument("--format", choices=["table", "json"], default="table")
    p_verify.set_defaults(handler=cmd_verify)

    p_reindex = sub.add_parser("reindex", parents=[common], help="从明细账重建索引")
    p_reindex.set_defaults(handler=cmd_reindex)

    p_export = sub.add_parser("export", parents=[common], help="导出账本")
    p_export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    p_export.add_argument("--kind", choices=[KIND_USAGE, KIND_AUTHORIZATION])
    p_export.add_argument("--out", help="输出文件，默认打印到标准输出")
    p_export.set_defaults(handler=cmd_export)

    p_history = sub.add_parser("history", parents=[common], help="账本的 git 提交历史")
    p_history.add_argument("--limit", type=int, default=20)
    p_history.set_defaults(handler=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_ERROR
    try:
        return handler(args)
    except (LedgerError, ConfigError, ReconcileError, ValueError) as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_OK
    finally:
        _close_ledgers()


if __name__ == "__main__":
    sys.exit(main())