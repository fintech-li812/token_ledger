"""来源日志的密码学证明：补上"入账之前有没有被动过"这一段。

账本里的哈希链只能证明**入账之后**没被改过。它证明不了**入账之前**源日志是否被改过。
attestation 就是补这一段：

* ``keygen`` 生成独立密钥（默认落在用户配置目录，**不进账本仓库**）
* ``sign``   对源日志的 SHA-256 做 HMAC-SHA256，并把这条证明作为一张凭证登记在案
* ``verify`` 入账时按文件路径找到证明，重算并比对

判定结果（会写进每张用量凭证的 ``source.attestation.status``）：

============ ==========================================================
``verified`` 有证明、文件哈希一致、HMAC 重算通过
``changed``  有证明但文件哈希变了 —— **日志在签名之后被改过**
``no_key``   有证明、哈希一致，但本机没有密钥，无法重算 HMAC
``invalid``  有证明、哈希一致，但 HMAC 对不上 —— 证明记录本身不自洽
``unsigned`` 没有任何证明覆盖这个文件
============ ==========================================================

**边界（必须说清）**：这仍然不是不可否认性证明。密钥在运维者手里，
它证明的是"这份日志自某时刻签名之后没变过"，以及"是哪个密钥签的"。
要防"跑 agent 的那台机器自己造假"，需要 agent 侧持有独立私钥并自行签名 —— 见 Roadmap。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from .models import iso_utc, sha256_file, sha256_text, utc_now

KEY_BYTES = 32
DEFAULT_KEY_FILENAME = "attestation.key"

STATUS_VERIFIED = "verified"
STATUS_CHANGED = "changed"
STATUS_NO_KEY = "no_key"
STATUS_INVALID = "invalid"
STATUS_UNSIGNED = "unsigned"

ALL_STATUSES = (STATUS_VERIFIED, STATUS_CHANGED, STATUS_NO_KEY, STATUS_INVALID, STATUS_UNSIGNED)

STATUS_TEXT = {
    STATUS_VERIFIED: "已证明",
    STATUS_CHANGED: "签名后被改动",
    STATUS_NO_KEY: "有证明但缺密钥",
    STATUS_INVALID: "证明不自洽",
    STATUS_UNSIGNED: "无证明",
}


class AttestationError(RuntimeError):
    """签名或校验失败。"""


def normalized_path(path: str | Path) -> str:
    """统一成绝对路径，避免同一个文件因为相对路径不同而被当成两个文件。"""
    return str(Path(path).expanduser().resolve())


def default_key_path() -> Path:
    """密钥默认放在用户配置目录，与账本仓库分离（不进版本控制）。"""
    from .config import user_config_path

    return user_config_path().parent / DEFAULT_KEY_FILENAME


def key_id(key: bytes) -> str:
    return sha256_text(key.hex())[:16]


def generate_key(path: str | Path | None = None, *, overwrite: bool = False):
    target = Path(path).expanduser() if path else default_key_path()
    if target.exists() and not overwrite:
        raise AttestationError(
            "密钥已存在：{0}（要覆盖请显式指定 --force；覆盖后旧签名将无法验证）".format(target)
        )
    key = secrets.token_bytes(KEY_BYTES)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(key.hex() + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target, key_id(key)


def load_key(path: str | Path | None = None) -> bytes:
    target = Path(path).expanduser() if path else default_key_path()
    if not target.is_file():
        raise AttestationError(
            "找不到密钥文件：{0}；先执行 tledger attest keygen".format(target)
        )
    try:
        return bytes.fromhex(target.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        raise AttestationError("密钥文件格式不正确：{0}".format(target)) from exc


def _mac(key: bytes, digest: str) -> str:
    return hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()


def sign_file(path: str | Path, key: bytes, *, signer: str | None = None,
              note: str | None = None) -> dict:
    """生成一条 attestation 凭证的 payload（由调用方 append 进账本）。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise AttestationError("要签名的文件不存在：{0}".format(target))
    digest = sha256_file(target)
    resolved = normalized_path(target)
    return {
        "signed_at": iso_utc(utc_now()),
        "occurred_day": iso_utc(utc_now())[:10],
        "subject": {"path": resolved, "sha256": digest, "size": target.stat().st_size},
        "hmac_sha256": _mac(key, digest),
        "key_id": key_id(key),
        "signer": signer,
        "note": note,
        "dedup_key": "attest:" + digest,
    }


def _latest_by_path(attestations) -> dict:
    """同一路径可能被多次签名（文件后来追加了内容），取最后登记的那条。"""
    latest: dict = {}
    for payload in attestations:
        subject = payload.get("subject") or {}
        path = subject.get("path")
        if path:
            latest[path] = payload
    return latest


def verify_file(path: str | Path, attestations, *, key: bytes | None = None) -> dict:
    """按路径核对某个文件是否有可信的来源证明。"""
    resolved = normalized_path(path)
    recorded = _latest_by_path(attestations)
    payload = recorded.get(resolved)

    if payload is None:
        return {
            "status": STATUS_UNSIGNED,
            "checked_path": resolved,
            "reason": "没有任何证明覆盖这个文件；无法判断它在入账前是否被改过",
        }

    subject = payload.get("subject") or {}
    try:
        digest = sha256_file(Path(path).expanduser())
    except OSError as exc:
        return {"status": STATUS_INVALID, "checked_path": resolved, "reason": str(exc)}

    if digest != subject.get("sha256"):
        return {
            "status": STATUS_CHANGED,
            "checked_path": resolved,
            "attested_sha256": subject.get("sha256"),
            "actual_sha256": digest,
            "key_id": payload.get("key_id"),
            "signed_at": payload.get("signed_at"),
            "reason": "日志在签名之后被改动过（哈希不一致）",
        }

    if key is None:
        return {
            "status": STATUS_NO_KEY,
            "checked_path": resolved,
            "sha256": digest,
            "key_id": payload.get("key_id"),
            "signed_at": payload.get("signed_at"),
            "reason": "文件哈希一致，但本机缺少密钥，无法重算 HMAC",
        }

    expected = payload.get("hmac_sha256")
    actual = _mac(key, digest)
    if not hmac.compare_digest(str(expected or ""), actual):
        return {
            "status": STATUS_INVALID,
            "checked_path": resolved,
            "sha256": digest,
            "key_id": payload.get("key_id"),
            "reason": "HMAC 校验失败：证明记录与密钥不匹配",
        }

    return {
        "status": STATUS_VERIFIED,
        "checked_path": resolved,
        "sha256": digest,
        "key_id": payload.get("key_id"),
        "signed_at": payload.get("signed_at"),
        "signer": payload.get("signer"),
        "reason": "文件哈希与 HMAC 均通过",
    }


def is_satisfied(status: str) -> bool:
    """策略判定：只有 verified 才算满足了"来源可信"的要求。"""
    return status == STATUS_VERIFIED