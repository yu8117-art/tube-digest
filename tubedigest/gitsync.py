"""처리 기록(data/)을 저장소에 커밋·push한다. GitHub Actions는 실행마다 새로 체크아웃하므로 이게 유일한 기억이다."""
from __future__ import annotations

import subprocess

from .config import ROOT


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def git_publish(message: str) -> bool:
    """data/ 변경분을 커밋하고, 아직 안 올라간 커밋이 있으면 push한다."""
    _git("add", "data")
    if _git("diff", "--cached", "--quiet").returncode != 0:
        c = _git("commit", "-m", message)
        if c.returncode != 0:
            raise RuntimeError(f"git commit 실패: {c.stderr.strip()}")
    ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return False
    # GitHub Actions가 먼저 기록을 커밋해 둔 경우 그 위에 얹는다 (PC에서 수동 실행할 때 대비)
    pull = _git("pull", "--rebase", "--autostash")
    if pull.returncode != 0:
        raise RuntimeError(f"git pull 실패: {pull.stderr.strip()}")
    p = _git("push")
    if p.returncode != 0:
        raise RuntimeError(f"git push 실패: {p.stderr.strip()}")
    return True
