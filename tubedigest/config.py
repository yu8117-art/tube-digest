"""설정 로딩: config.yaml(동작 설정) + channels.yaml(구독 채널) + .env(비밀값)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
CHANNELS_PATH = ROOT / "channels.yaml"
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
PROMPT_PATH = ROOT / "prompts" / "summary_prompt.md"


def load_dotenv(path: Path = ENV_PATH) -> None:
    """KEY=VALUE 형식의 .env를 읽어 환경변수에 넣는다 (이미 설정된 값은 덮어쓰지 않음).

    윈도우 탐색기에서 확장자가 숨겨진 채 만들면 '.env.txt'가 되므로 그것도 받아준다.
    """
    if not path.exists():
        path = path.with_name(".env.txt")
        if not path.exists():
            return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Channel:
    id: str
    name: str = ""


@dataclass
class Config:
    channels: list[Channel]
    skip_shorts: bool = True
    skip_live: bool = True
    max_video_minutes: int = 180
    max_videos_per_run: int = 5
    run_budget_minutes: int = 35
    backfill_on_first_run: int = 0
    git_push: bool = False
    gemini_model: str = "gemini-3.8-flash"
    gemini_segment_minutes: int = 30
    gemini_pause_sec: int = 60
    gemini_call_timeout_minutes: int = 10
    gemini_wait_hours: float = 24
    notify_provider: str = "none"
    ntfy_server: str = "https://ntfy.sh"

    @property
    def secrets(self) -> dict[str, str]:
        keys = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "NTFY_TOKEN")
        return {k: os.environ.get(k, "") for k in keys}


def load_channels() -> list[Channel]:
    if not CHANNELS_PATH.exists():
        return []
    raw = yaml.safe_load(CHANNELS_PATH.read_text(encoding="utf-8")) or {}
    return [Channel(id=c["id"], name=c.get("name", "")) for c in raw.get("channels") or []]


def save_channels(channels: list[Channel]) -> None:
    body = {"channels": [{"id": c.id, "name": c.name} for c in channels]}
    header = "# 구독 채널 목록. `.\\td add \"@핸들\"` 로 추가하거나 직접 편집해도 된다.\n"
    CHANNELS_PATH.write_text(header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")


def load_config() -> Config:
    load_dotenv()
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    opt, gm, nt = (raw.get(k) or {} for k in ("options", "gemini", "notify"))
    return Config(
        channels=load_channels(),
        skip_shorts=opt.get("skip_shorts", True),
        skip_live=opt.get("skip_live", True),
        max_video_minutes=int(opt.get("max_video_minutes", 180)),
        max_videos_per_run=int(opt.get("max_videos_per_run", 5)),
        run_budget_minutes=int(opt.get("run_budget_minutes", 35)),
        backfill_on_first_run=int(opt.get("backfill_on_first_run", 0)),
        git_push=bool(opt.get("git_push", False)),
        gemini_model=gm.get("model", "gemini-3.8-flash"),
        gemini_segment_minutes=int(gm.get("segment_minutes", 30)),
        gemini_pause_sec=int(gm.get("pause_sec", 60)),
        gemini_call_timeout_minutes=int(gm.get("call_timeout_minutes", 10)),
        gemini_wait_hours=float(gm.get("wait_hours", 24)),
        notify_provider=nt.get("provider", "none"),
        ntfy_server=(nt.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
    )
