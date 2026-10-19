"""저장소와 동기화: 처리 기록(data/)·채널 목록을 커밋·push하고, 최신 상태를 받아온다.

GitHub Actions는 실행마다 저장소를 새로 받으므로, 여기 커밋된 것이 Actions가 아는 전부다.
"""
from __future__ import annotations

import subprocess

from .config import ROOT


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def git_pull() -> None:
    """원격의 최신 커밋을 받는다. 작업 중인 변경은 잠시 치워 두었다가 다시 얹는다."""
    p = _git("pull", "--rebase", "--autostash")
    if p.returncode != 0:
        raise RuntimeError(f"git pull 실패: {p.stderr.strip()}")


def git_publish(message: str, paths: tuple[str, ...] = ("data",)) -> bool:
    """paths의 변경분만 커밋하고, 아직 안 올라간 커밋이 있으면 push한다. push했으면 True."""
    _git("add", "--", *paths)
    if _git("diff", "--cached", "--quiet", "--", *paths).returncode != 0:
        c = _git("commit", "-m", message, "--", *paths)  # 다른 파일의 변경은 섞지 않는다
        if c.returncode != 0:
            raise RuntimeError(f"git commit 실패: {c.stderr.strip()}")
    ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return False
    # GitHub Actions가 먼저 기록을 커밋해 둔 경우 그 위에 얹는다
    git_pull()
    p = _git("push")
    if p.returncode != 0:
        raise RuntimeError(f"git push 실패: {p.stderr.strip()}")
    return True
