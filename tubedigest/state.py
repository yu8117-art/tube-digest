"""처리 상태 저장소 (data/state.json).

JSON 파일 하나로 둔 이유: GitHub Actions로 돌릴 때도 커밋만 하면 상태가 유지되고, diff로 보기 쉽다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

STATE_PATH = DATA_DIR / "state.json"
ARTICLES_DIR = DATA_DIR / "articles"
LOCK_PATH = DATA_DIR / "run.lock"

# 영상 상태
PENDING = "pending"          # 요약 대기 (자막 기다리는 중 포함)
DONE = "done"                # 기사 발행 완료
BASELINE = "baseline"        # 채널 첫 등록 시 이미 있던 영상 (요약 안 함)
SKIPPED_SHORT = "skipped_short"
FAILED = "failed"            # 재시도 한도 초과


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, data: dict):
        self.data = data
        self.data.setdefault("channels", {})
        self.data.setdefault("videos", {})

    @classmethod
    def load(cls) -> "State":
        if STATE_PATH.exists():
            return cls(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        return cls({})

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_PATH)

    @property
    def videos(self) -> dict[str, dict]:
        return self.data["videos"]

    def channel_initialized(self, cid: str) -> bool:
        return self.data["channels"].get(cid, {}).get("initialized", False)

    def mark_channel_initialized(self, cid: str) -> None:
        self.data["channels"].setdefault(cid, {})["initialized"] = now_iso()

    def pending(self) -> list[tuple[str, dict]]:
        items = [(vid, v) for vid, v in self.videos.items() if v["status"] == PENDING]
        return sorted(items, key=lambda kv: kv[1].get("published", ""))

    def unnotified(self) -> list[tuple[str, dict]]:
        return [(vid, v) for vid, v in self.videos.items() if v["status"] == DONE and not v.get("notified")]


def save_article(video_id: str, article: dict) -> None:
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    (ARTICLES_DIR / f"{video_id}.json").write_text(
        json.dumps(article, ensure_ascii=False, indent=1), encoding="utf-8")


def load_article(video_id: str) -> dict:
    return json.loads((ARTICLES_DIR / f"{video_id}.json").read_text(encoding="utf-8"))


def load_all_articles() -> list[dict]:
    if not ARTICLES_DIR.exists():
        return []
    arts = [json.loads(p.read_text(encoding="utf-8")) for p in ARTICLES_DIR.glob("*.json")]
    return sorted(arts, key=lambda a: a["video"]["published"], reverse=True)


class RunLock:
    """작업 스케줄러가 이전 실행이 끝나기 전에 다시 띄워도 겹치지 않게 한다. 1시간 지난 락은 무시."""

    def __init__(self, path: Path = LOCK_PATH, stale_sec: int = 3600):
        self.path, self.stale_sec = path, stale_sec

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale_sec:
            self.path.unlink()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit("다른 실행이 진행 중이라 종료함 (data/run.lock)")
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
