"""git 集成：账本的版本化留痕。只做 add / commit / log，永远不 push。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEFAULT_GITIGNORE = """# tokenledger 账本仓库
# 派生索引与运行期锁文件不是账本真相，不纳入版本控制
ledger/index.sqlite
ledger/index.sqlite-journal
ledger/index.sqlite-wal
ledger/index.sqlite-shm
*.lock
"""


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def is_repo(path: str | Path) -> bool:
    return (Path(path) / ".git").exists()


def init_repo(path: str | Path) -> dict:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8")

    if not git_available():
        return {"ok": False, "initialized": False, "reason": "未找到 git 可执行文件，跳过仓库初始化"}
    if is_repo(path):
        return {"ok": True, "initialized": False, "reason": "已是 git 仓库"}

    result = _run(["init", "-b", "main"], path)
    if result.returncode != 0:
        result = _run(["init"], path)
        if result.returncode == 0:
            _run(["symbolic-ref", "HEAD", "refs/heads/main"], path)
    return {
        "ok": result.returncode == 0,
        "initialized": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def commit_all(path: str | Path, message: str) -> dict:
    path = Path(path).expanduser()
    if not git_available():
        return {"ok": False, "committed": False, "reason": "未找到 git 可执行文件"}
    if not is_repo(path):
        return {"ok": False, "committed": False, "reason": "不是 git 仓库"}

    _run(["add", "-A"], path)
    staged = _run(["status", "--porcelain"], path)
    if not staged.stdout.strip():
        return {"ok": True, "committed": False, "reason": "无改动"}

    result = _run(["commit", "-m", message], path)
    if result.returncode != 0:
        # 用户没配 git 身份时兜底，避免因为提交失败而中断记账
        combined = (result.stderr or "") + (result.stdout or "")
        if "user.email" in combined or "user.name" in combined or "Author identity" in combined:
            result = _run(
                [
                    "-c",
                    "user.name=tokenledger",
                    "-c",
                    "user.email=tokenledger@localhost",
                    "commit",
                    "-m",
                    message,
                ],
                path,
            )
    return {
        "ok": result.returncode == 0,
        "committed": result.returncode == 0,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def log(path: str | Path, limit: int = 10) -> list[str]:
    if not git_available() or not is_repo(path):
        return []
    result = _run(["log", "--oneline", "-n", str(limit)], path)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]