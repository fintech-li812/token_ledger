"""账本主体：只增不改的凭证流水 + 哈希链 + 可随时重建的派生索引。

三条不可动摇的规则：

1. ``ledger/*.jsonl`` 是唯一真相，SQLite 只是查询加速（丢了能重建）。
2. 账本只增不改：没有 update / delete 命令，冲正只能追加反向凭证（红字冲销）。
3. 每条凭证带前序哈希，事后任何修改都会被 ``verify`` 检出。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import repo as gitrepo
from .attestation import STATUS_CHANGED, STATUS_INVALID
from .config import CONFIG_FILENAME, CONFIG_TEMPLATE, Config
from .models import (
    GENESIS_HASH,
    KIND_ATTESTATION,
    KIND_AUTHORIZATION,
    KIND_USAGE,
    PREFIX_KIND,
    Usage,
    canonical_json,
    date_str,
    iso_utc,
    make_record,
    make_voucher_no,
    parse_iso,
    parse_voucher_no,
    seal,
    utc_now,
    verify_hash,
)
from .store import Index

STREAM_FILES = {
    KIND_USAGE: "usage.jsonl",
    KIND_AUTHORIZATION: "authorization.jsonl",
    KIND_ATTESTATION: "attestation.jsonl",
}
LOCK_FILENAME = "ledger.lock"
LOCK_STALE_SECONDS = 300
LOCK_WAIT_SECONDS = 10.0


class LedgerError(RuntimeError):
    """账本读写失败。"""


class DuplicateVoucher(LedgerError):
    """同一笔业务被重复入账（幂等保护）。"""


class LockedLedger(LedgerError):
    """账本正被另一个进程写入。"""


@contextmanager
def repo_lock(path: Path, stale_after: int = LOCK_STALE_SECONDS, wait: float = LOCK_WAIT_SECONDS):
    """跨进程互斥：保证"读上一条哈希 → 追加 → 更新索引"是原子的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    handle = None
    while handle is None:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise LockedLedger(
                    "账本被锁定：{0}；若确认没有其他进程在写入，删除该文件即可".format(path)
                )
            time.sleep(0.1)
    try:
        os.write(handle, str(os.getpid()).encode("utf-8"))
        os.close(handle)
        handle = None
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def build_usage_payload(
    *,
    agent_id: str | None,
    model: str | None,
    usage: Usage,
    occurred_at: Any = None,
    provider: str | None = None,
    agent_version: str | None = None,
    session: dict | None = None,
    project: str | None = None,
    source: dict | None = None,
    cost: dict | None = None,
    budget: dict | None = None,
    cumulative: dict | None = None,
    dedup_key: str | None = None,
    recorded_by: str | None = None,
    approved_by: str | None = None,
) -> dict:
    """把一次用量组装成凭证 payload。字段命名与 README 中的示例一致。"""
    moment = parse_iso(occurred_at) or utc_now()
    return {
        "occurred_at": iso_utc(moment),
        "occurred_day": date_str(moment),
        "agent": {"id": agent_id, "provider": provider, "version": agent_version},
        "model": model,
        "project": project,
        "session": session or {},
        "usage": usage.to_dict(),
        "usage_cumulative": cumulative,
        "cost": cost,
        "budget": budget,
        "source": source,
        "dedup_key": dedup_key,
        "recorded_by": recorded_by,
        "approved_by": approved_by,
    }


class Ledger:
    def __init__(self, root: str | Path, config: Config):
        self.root = Path(root).expanduser().resolve()
        self.config = config
        self.dir = config.ledger_dir
        self._index: Index | None = None

    # ------------------------------------------------------------------ #
    # 路径与生命周期
    # ------------------------------------------------------------------ #
    def stream_path(self, kind: str = KIND_USAGE) -> Path:
        return self.dir / STREAM_FILES.get(kind, "{0}.jsonl".format(kind))

    @property
    def index_path(self) -> Path:
        return self.dir / "index.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK_FILENAME

    @property
    def index(self) -> Index:
        if self._index is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._index = Index(self.index_path)
        return self._index

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @classmethod
    def init(cls, root: str | Path, *, git: bool = True, force: bool = False) -> dict:
        """建账本：配置 + 两个空的凭证流水 + 空索引（+ 可选 git 仓库）。"""
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        config_file = root / CONFIG_FILENAME
        config_written = False
        if force or not config_file.exists():
            config_file.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            config_written = True

        config = Config.load(root)
        ledger_dir = config.ledger_dir
        ledger_dir.mkdir(parents=True, exist_ok=True)
        streams = {}
        for kind, filename in STREAM_FILES.items():
            path = ledger_dir / filename
            if not path.exists():
                path.write_text("", encoding="utf-8")
            streams[kind] = str(path)

        ledger = cls(root, config)
        probe = Index(ledger.index_path)
        probe.close()

        result = {
            "root": str(root),
            "config": str(config_file),
            "config_written": config_written,
            "ledger_dir": str(ledger_dir),
            "streams": streams,
        }
        if git:
            result["git"] = gitrepo.init_repo(root)
            if config.auto_commit:
                result["commit"] = gitrepo.commit_all(root, "chore: initialize token ledger")
        return result

    @classmethod
    def open(cls, root: str | Path) -> "Ledger":
        root = Path(root).expanduser().resolve()
        if not (root / CONFIG_FILENAME).is_file():
            raise LedgerError(
                "{0} 不是账本目录（缺少 {1}）；先执行 tledger init".format(root, CONFIG_FILENAME)
            )
        return cls(root, Config.load(root))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def read_records(self, kind: str = KIND_USAGE, *, strict: bool = True) -> Iterator[dict]:
        path = self.stream_path(kind)
        if not path.is_file():
            return
        with open(path, "r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    yield json.loads(text)
                except json.JSONDecodeError as exc:
                    if strict:
                        raise LedgerError(
                            "{0}:{1} 无法解析（账本可能损坏）：{2}".format(path, lineno, exc)
                        ) from exc

    def count(self, kind: str = KIND_USAGE) -> int:
        return sum(1 for _ in self.read_records(kind))

    def last_record(self, kind: str = KIND_USAGE) -> dict | None:
        last = None
        for record in self.read_records(kind):
            last = record
        return last

    def dedup_keys(self, kind: str = KIND_USAGE) -> set:
        keys = set()
        for record in self.read_records(kind):
            value = (record.get("payload") or {}).get("dedup_key")
            if value:
                keys.add(str(value))
        return keys

    def attestations(self) -> list[dict]:
        """全部来源证明（用于入账时核对）。"""
        return [item["payload"] for item in self.index.attestations()]

    def has_dedup_key(self, kind: str, dedup_key: str | None) -> bool:
        if not dedup_key:
            return False
        return str(dedup_key) in self.dedup_keys(kind)

    # ------------------------------------------------------------------ #
    # 写入（只增）
    # ------------------------------------------------------------------ #
    def append(
        self,
        kind: str,
        payload: dict,
        *,
        recorded_at: Any = None,
        dedup_key: str | None = None,
        existing_keys: set | None = None,
        commit_message: str | None = None,
    ) -> dict:
        recorded_at = iso_utc(parse_iso(recorded_at) or utc_now())
        year = (parse_iso(recorded_at) or utc_now()).year
        with repo_lock(self.lock_path):
            if dedup_key:
                duplicated = (
                    str(dedup_key) in existing_keys
                    if existing_keys is not None
                    else self.has_dedup_key(kind, dedup_key)
                )
                if duplicated:
                    raise DuplicateVoucher(
                        "凭证已存在（dedup_key={0}），拒绝重复入账".format(dedup_key)
                    )

            last = self.last_record(kind)
            if last is None:
                prev_hash, seq = GENESIS_HASH, 1
            else:
                prev_hash = last.get("hash") or GENESIS_HASH
                parsed = parse_voucher_no(last.get("voucher_no"))
                seq = parsed[2] + 1 if (parsed and parsed[1] == year) else 1

            record = make_record(
                voucher_no=make_voucher_no(kind, year, seq),
                kind=kind,
                recorded_at=recorded_at,
                prev_hash=prev_hash,
                payload=payload,
            )
            seal(record)

            path = self.stream_path(kind)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            self.index.add(record, dedup_key=dedup_key)
            self.index.set_seq(kind, year, seq)
        if commit_message and self.config.auto_commit:
            gitrepo.commit_all(self.root, commit_message)
        return record

    # ------------------------------------------------------------------ #
    # 审计与重建
    # ------------------------------------------------------------------ #
    def verify(self) -> dict:
        """哈希链完整性 + 凭证号连续性 + 索引一致性 + 会话账实核对。"""
        errors: list[str] = []
        kinds: dict[str, dict] = {}

        for kind in STREAM_FILES:
            prev_hash = GENESIS_HASH
            count = 0
            expected_seq: dict[tuple[str, int], int] = {}
            for record in self.read_records(kind):
                count += 1
                label = str(record.get("voucher_no") or "#{0}".format(count))
                if not verify_hash(record):
                    errors.append("{0} 内容被篡改：hash 与实际内容不符".format(label))
                previous = record.get("prev_hash")
                if count == 1 and previous != GENESIS_HASH:
                    errors.append("{0} 首条凭证的 prev_hash 不是创世哈希".format(label))
                elif count > 1 and previous != prev_hash:
                    errors.append("{0} 哈希链断裂：prev_hash 与上一条凭证不符".format(label))
                prev_hash = record.get("hash") or ""

                parsed = parse_voucher_no(record.get("voucher_no"))
                if parsed is None:
                    errors.append("{0} 凭证号格式非法".format(label))
                else:
                    prefix, year, seq = parsed
                    key = (prefix, year)
                    expected = expected_seq.get(key, 1)
                    if seq != expected:
                        errors.append(
                            "{0} 凭证号不连续：应为 {1}（可能被删除或重复入账）".format(
                                label,
                                make_voucher_no(PREFIX_KIND.get(prefix, kind), year, expected),
                            )
                        )
                    expected_seq[key] = seq + 1
            kinds[kind] = {"vouchers": count, "head_hash": prev_hash}

        # 索引 vs 明细账
        expected_hashes = {}
        for kind in STREAM_FILES:
            for record in self.read_records(kind):
                expected_hashes[record.get("voucher_no")] = record.get("hash")
        actual_hashes = self.index.record_hashes()
        missing = sorted(set(expected_hashes) - set(actual_hashes))
        extra = sorted(set(actual_hashes) - set(expected_hashes))
        mismatched = sorted(
            no
            for no in set(expected_hashes) & set(actual_hashes)
            if expected_hashes[no] != actual_hashes[no]
        )
        index_report = {
            "jsonl_vouchers": len(expected_hashes),
            "index_vouchers": len(actual_hashes),
            "missing_in_index": missing[:20],
            "extra_in_index": extra[:20],
            "mismatched": mismatched[:20],
        }
        if missing:
            errors.append(
                "索引缺少 {0} 条凭证（明细账有、索引无），执行 tledger reindex 重建".format(len(missing))
            )
        if extra:
            errors.append("索引多出 {0} 条凭证（索引有、明细账无）".format(len(extra)))
        if mismatched:
            errors.append("索引与明细账有 {0} 条内容不一致".format(len(mismatched)))

        # 会话级账实核对：明细逐笔累加 是否等于 日志里的累计值
        sessions: dict[str, dict] = {}
        partial_sessions: set = set()
        for record in self.read_records(KIND_USAGE):
            payload = record.get("payload") or {}
            session = payload.get("session") or {}
            session_id = session.get("id")
            if not session_id:
                continue
            if session.get("partial"):
                partial_sessions.add(session_id)
            acc = sessions.setdefault(
                session_id, {"delta_total_tokens": 0, "cumulative_total_tokens": 0, "vouchers": 0}
            )
            acc["delta_total_tokens"] += int((payload.get("usage") or {}).get("total_tokens") or 0)
            acc["vouchers"] += 1
            cumulative = (payload.get("usage_cumulative") or {}).get("total_tokens")
            if cumulative is not None:
                acc["cumulative_total_tokens"] = max(acc["cumulative_total_tokens"], int(cumulative))

        session_totals = []
        for session_id, acc in sorted(sessions.items()):
            entry = dict(acc)
            entry["session_id"] = session_id
            if session_id in partial_sessions:
                entry["status"] = "partial"
            elif acc["cumulative_total_tokens"] == 0:
                entry["status"] = "no_cumulative"
            elif acc["delta_total_tokens"] == acc["cumulative_total_tokens"]:
                entry["status"] = "matched"
            else:
                entry["status"] = "mismatch"
                errors.append(
                    "会话 {0} 账实不符：明细累加 {1} != 日志累计 {2}".format(
                        session_id, acc["delta_total_tokens"], acc["cumulative_total_tokens"]
                    )
                )
            session_totals.append(entry)

        # 来源证明覆盖情况：changed / invalid 是实打实的篡改信号，必须让审计失败
        attestation_rows = self.index.aggregate(group_by="attestation", kind=KIND_USAGE)
        attestation_report = []
        for row in attestation_rows:
            status = row.get("grp")
            attestation_report.append(
                {"status": status, "vouchers": int(row.get("vouchers") or 0)}
            )
            if status in (STATUS_CHANGED, STATUS_INVALID):
                errors.append(
                    "有 {0} 张凭证的来源日志被改动过或证明不自洽（状态 {1}）".format(
                        row.get("vouchers"), status
                    )
                )

        return {
            "ok": not errors,
            "errors": errors,
            "kinds": kinds,
            "index": index_report,
            "session_totals": session_totals,
            "attestations": attestation_report,
        }

    def reindex(self) -> dict:
        """丢弃并重建 SQLite 索引：账实核对的兜底手段。"""
        with repo_lock(self.lock_path):
            index = self.index
            index.clear_vouchers()
            for kind in STREAM_FILES:
                last_seq_by_year: dict[int, int] = {}
                for record in self.read_records(kind):
                    payload = record.get("payload") or {}
                    index.add(record, dedup_key=payload.get("dedup_key"))
                    parsed = parse_voucher_no(record.get("voucher_no"))
                    if parsed:
                        last_seq_by_year[parsed[1]] = parsed[2]
                for year, seq in last_seq_by_year.items():
                    index.set_seq(kind, year, seq)
        return {"vouchers": self.index.counts()}