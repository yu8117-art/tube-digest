"""기사 HTML 생성 + GitHub Pages 배포(git push) + 배포 완료 대기."""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT, Config
from .state import load_all_articles
from .youtube import fmt_ts

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋으로 충분
INDEX_LIMIT = 100


def _kdate(iso: str) -> str:
    d = datetime.fromisoformat(iso).astimezone(KST)
    return f"{d.year}년 {d.month}월 {d.day}일 {d:%H:%M}"


_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["j2", "html"]),
    trim_blocks=True, lstrip_blocks=True,
)
_env.filters["kdate"] = _kdate
_env.filters["ts"] = fmt_ts


def article_url(cfg: Config, video_id: str) -> str:
    return f"{cfg.base_url}/a/{video_id}.html"


def render_article(cfg: Config, article: dict) -> Path:
    out = cfg.output_dir / "a" / f"{article['video']['video_id']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_env.get_template("article.html.j2").render(a=article), encoding="utf-8")
    return out


def render_index(cfg: Config) -> Path:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.output_dir / ".nojekyll").touch()  # GitHub Pages가 파일을 가공하지 않게
    out = cfg.output_dir / "index.html"
    html = _env.get_template("index.html.j2").render(
        articles=load_all_articles()[:INDEX_LIMIT],
        updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    out.write_text(html, encoding="utf-8")
    return out


def rebuild_all(cfg: Config) -> int:
    arts = load_all_articles()
    for a in arts:
        render_article(cfg, a)
    render_index(cfg)
    return len(arts)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def git_publish(message: str) -> bool:
    """docs/ 와 data/ 변경분을 커밋하고, 아직 안 올라간 커밋이 있으면 push한다."""
    _git("add", "docs", "data")
    if _git("diff", "--cached", "--quiet").returncode != 0:
        c = _git("commit", "-m", message)
        if c.returncode != 0:
            raise RuntimeError(f"git commit 실패: {c.stderr.strip()}")
    ahead = _git("rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return False
    # GitHub Actions가 먼저 상태를 커밋해 둔 경우 그 위에 얹는다 (PC에서 수동 실행할 때 대비)
    pull = _git("pull", "--rebase", "--autostash")
    if pull.returncode != 0:
        raise RuntimeError(f"git pull 실패: {pull.stderr.strip()}")
    p = _git("push")
    if p.returncode != 0:
        raise RuntimeError(f"git push 실패: {p.stderr.strip()}")
    return True


def wait_until_live(url: str, timeout_sec: int) -> bool:
    """Pages 배포가 끝나 기사 URL이 200을 줄 때까지 기다린다. 알림을 눌렀는데 404가 뜨는 걸 막는다."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if requests.head(url, timeout=10).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(10)
    log.warning("배포 대기 시간 초과, 그대로 알림 전송: %s", url)
    return False
