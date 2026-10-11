"""스마트폰 알림: 요약 전문을 Telegram 메시지(기본) 또는 ntfy로 보낸다."""
from __future__ import annotations

import html
import re

import requests

from .config import Config
from .youtube import fmt_ts

TG_LIMIT = 4096  # 텔레그램 메시지 한 개의 최대 글자 수 (태그 제외한 보이는 글자 기준)


class NotifyError(Exception):
    pass


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _visible_len(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def notify(cfg: Config, article: dict) -> None:
    if cfg.notify_provider == "telegram":
        _telegram(cfg, article)
    elif cfg.notify_provider == "ntfy":
        _ntfy(cfg, article)
    elif cfg.notify_provider != "none":
        raise NotifyError(f"알 수 없는 notify.provider: {cfg.notify_provider}")


def telegram_messages(article: dict) -> list[str]:
    """요약을 텔레그램 HTML 메시지로 만든다. 길면 [본문, 타임라인] 두 개로 나눈다."""
    v, s = article["video"], article["summary"]
    e = lambda text: html.escape(text, quote=False)  # 본문은 <, >, &만 바꾼다 (따옴표는 그대로)
    yt = v["url"]
    head = [f"📺 <b>{e(v['channel_name'])}</b> 새 영상",
            f'<a href="{html.escape(yt)}"><b>{e(v["title"])}</b></a>']
    if article.get("duration"):
        head[-1] += f" ({fmt_ts(article['duration'])})"
    head += ["", f"📰 <b>{e(s['headline'])}</b>"]
    if article.get("source") == "metadata":
        head.append("⚠️ 영상 분석 실패 · 제목/설명/썸네일 기반 요약")
    thumb_text = " / ".join(x.strip() for x in s["thumbnail_text"].splitlines() if x.strip())
    body = head + [
        "", "🖼 <b>썸네일</b>", f"“{e(thumb_text)}”", f"→ {e(s['thumbnail_answer'])}",
        "", "<b>1. 결론</b>", e(s["conclusion"]),
        "", "<b>핵심 요약</b>", *[f"• {e(p)}" for p in s["key_points"]],
    ]
    items = []
    for t in s.get("timeline", []):
        sec = int(t["seconds"])
        item = f'<a href="{html.escape(f"{yt}&t={sec}s")}">{fmt_ts(sec)}</a> <b>{e(t["title"])}</b>'
        item += "".join(f"\n  - {e(p)}" for p in t.get("points", []))
        items.append(item)
    tags = " ".join(f"#{e(t.replace(' ', '_'))}" for t in s.get("tags", [])[:6])

    def timeline(chunk: list[str], cont: bool = False) -> str:
        title = "<b>2. 타임라인 (이어서)</b>" if cont else "<b>2. 타임라인</b>"
        return f"{title}\n<blockquote expandable>" + "\n".join(chunk) + "</blockquote>"

    tail = "\n\n".join(x for x in [timeline(items) if items else "", tags] if x)
    one = "\n".join(body) + ("\n\n" + tail if tail else "")
    if _visible_len(one) <= TG_LIMIT:
        return [one]
    # 길면 본문과 타임라인을 나누고, 타임라인도 한도에 맞춰 여러 메시지로 이어 보낸다 (내용은 줄이지 않는다)
    msgs, chunk = ["\n".join(body)], []
    for item in items:
        if chunk and _visible_len(timeline(chunk + [item], len(msgs) > 1)) > TG_LIMIT - 200:
            msgs.append(timeline(chunk, len(msgs) > 1))
            chunk = []
        chunk.append(item)
    last = timeline(chunk, len(msgs) > 1) if chunk else ""
    msgs.append("\n\n".join(x for x in [last, tags] if x))
    return msgs


def _telegram(cfg: Config, article: dict) -> None:
    token, chat_id = cfg.secrets["TELEGRAM_BOT_TOKEN"], cfg.secrets["TELEGRAM_CHAT_ID"]
    if not token or not chat_id:
        raise NotifyError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없음 (.env 또는 GitHub Secrets)")
    yt = article["video"]["url"]
    for i, text in enumerate(telegram_messages(article)):
        # 첫 메시지에는 유튜브 링크 미리보기(썸네일)를 본문 위에 크게 띄운다
        preview = ({"url": yt, "prefer_large_media": True, "show_above_text": True} if i == 0
                   else {"is_disabled": True})
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": preview},
            timeout=20,
        )
        if not r.ok:
            raise NotifyError(f"Telegram 전송 실패 {r.status_code}: {r.text[:200]}")


def _ntfy(cfg: Config, article: dict) -> None:
    topic = cfg.secrets["NTFY_TOPIC"]
    if not topic:
        raise NotifyError("NTFY_TOPIC이 없음 (.env 또는 GitHub Secrets)")
    v, s = article["video"], article["summary"]
    headers = {}
    if cfg.secrets["NTFY_TOKEN"]:
        headers["Authorization"] = f"Bearer {cfg.secrets['NTFY_TOKEN']}"
    lines = [f"썸네일: {s['thumbnail_text']}", f"→ {s['thumbnail_answer']}", "", "[결론]", s["conclusion"], "",
             "[핵심 요약]", *[f"• {p}" for p in s["key_points"]], "", "[타임라인]",
             *[f"{fmt_ts(t['seconds'])} {t['title']}" for t in s.get("timeline", [])]]
    r = requests.post(
        cfg.ntfy_server,
        json={
            "topic": topic,
            "title": f"[{v['channel_name']}] {s['headline']}",
            "message": _clip("\n".join(lines), 3800),
            "click": v["url"],
            "attach": article.get("thumbnail", f"https://i.ytimg.com/vi/{v['video_id']}/hqdefault.jpg"),
            "tags": ["tv"],
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
