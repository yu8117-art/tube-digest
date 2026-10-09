"""스마트폰 푸시 알림: Telegram 봇(기본) 또는 ntfy."""
from __future__ import annotations

import html

import requests

from .config import Config


class NotifyError(Exception):
    pass


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def notify(cfg: Config, article: dict, url: str) -> None:
    v, s = article["video"], article["summary"]
    if cfg.notify_provider == "telegram":
        _telegram(cfg, v, s, article["thumbnail"], url)
    elif cfg.notify_provider == "ntfy":
        _ntfy(cfg, v, s, article["thumbnail"], url)
    elif cfg.notify_provider != "none":
        raise NotifyError(f"알 수 없는 notify.provider: {cfg.notify_provider}")


def _telegram(cfg: Config, v: dict, s: dict, thumb: str, url: str) -> None:
    token, chat_id = cfg.secrets["TELEGRAM_BOT_TOKEN"], cfg.secrets["TELEGRAM_CHAT_ID"]
    if not token or not chat_id:
        raise NotifyError(".env에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없음")
    e = html.escape
    caption = (
        f"📺 <b>{e(v['channel_name'])}</b> 새 영상\n"
        f"<b>{e(s['headline'])}</b>\n\n"
        f"💬 {e(_clip(s['thumbnail_answer'], 220))}\n\n"
        f"{e(_clip(s['conclusion'], 420))}\n\n"
        + " ".join(f"#{e(t.replace(' ', '_'))}" for t in s.get("tags", [])[:5])
    )
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        json={
            "chat_id": chat_id,
            "photo": thumb,
            "caption": _clip(caption, 1024),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "📰 요약 기사 보기", "url": url},
                {"text": "▶ 유튜브", "url": v["url"]},
            ]]},
        },
        timeout=20,
    )
    if not r.ok:
        raise NotifyError(f"Telegram 전송 실패 {r.status_code}: {r.text[:200]}")


def _ntfy(cfg: Config, v: dict, s: dict, thumb: str, url: str) -> None:
    topic = cfg.secrets["NTFY_TOPIC"]
    if not topic:
        raise NotifyError(".env에 NTFY_TOPIC이 없음")
    headers = {}
    if cfg.secrets["NTFY_TOKEN"]:
        headers["Authorization"] = f"Bearer {cfg.secrets['NTFY_TOKEN']}"
    r = requests.post(
        cfg.ntfy_server,
        json={
            "topic": topic,
            "title": f"[{v['channel_name']}] {s['headline']}",
            "message": _clip(f"{s['thumbnail_answer']}\n\n{s['conclusion']}", 900),
            "click": url,
            "attach": thumb,
            "tags": ["tv"],
            "actions": [{"action": "view", "label": "유튜브", "url": v["url"]}],
        },
        headers=headers,
        timeout=20,
    )
    if not r.ok:
        raise NotifyError(f"ntfy 전송 실패 {r.status_code}: {r.text[:200]}")


def telegram_chat_ids(token: str) -> list[tuple[str, str]]:
    """봇에게 아무 메시지나 보낸 뒤 호출하면 (chat_id, 이름) 목록을 돌려준다."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    r.raise_for_status()
    seen = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    return list(seen.items())
