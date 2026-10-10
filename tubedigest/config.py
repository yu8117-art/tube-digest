"""설정 로딩: config.yaml(동작 설정) + channels.yaml(구독 채널) + .env(비밀값)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    transcript_languages: list[str] = field(default_factory=lambda: ["ko", "en"])
    transcript_wait_hours: float = 6
    max_videos_per_run: int = 5
    backfill_on_first_run: int = 0
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    base_url: str = ""
    output_dir: Path = ROOT / "docs"
    git_push: bool = False
    wait_until_live_sec: int = 180
    notify_provider: str = "none"
    ntfy_server: str = "https://ntfy.sh"
    analysis_provider: str = "none"
    analysis_model: str = "gemini-3.8-flash"
    analysis_segment_minutes: int = 30
    analysis_pause_sec: int = 60
    analysis_wait_hours: float = 24

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
    opt, cl, site, nt, va = (raw.get(k) or {} for k in ("options", "claude", "site", "notify", "video_analysis"))
    return Config(
        channels=load_channels(),
        skip_shorts=opt.get("skip_shorts", True),
        transcript_languages=opt.get("transcript_languages", ["ko", "en"]),
        transcript_wait_hours=float(opt.get("transcript_wait_hours", 6)),
        max_videos_per_run=int(opt.get("max_videos_per_run", 5)),
        backfill_on_first_run=int(opt.get("backfill_on_first_run", 0)),
        model=cl.get("model", "claude-opus-5-5"),
        effort=cl.get("effort", "medium"),
        base_url=(site.get("base_url") or "").rstrip("/"),
        output_dir=ROOT / site.get("output_dir", "docs"),
        git_push=bool(site.get("git_push", False)),
        wait_until_live_sec=int(site.get("wait_until_live_sec", 180)),
        notify_provider=nt.get("provider", "none"),
        ntfy_server=(nt.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
        analysis_provider=va.get("provider", "none"),
        analysis_model=va.get("model", "gemini-3.8-flash"),
        analysis_segment_minutes=int(va.get("segment_minutes", 30)),
        analysis_pause_sec=int(va.get("pause_sec", 60)),
        analysis_wait_hours=float(va.get("wait_hours", 24)),
    )
