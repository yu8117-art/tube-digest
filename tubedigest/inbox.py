"""텔레그램으로 받은 유튜브 링크를 요약해서 답장한다. 두 가지 방식이 있다.

- 웹훅(기본): 봇에 메시지가 오면 텔레그램 → Cloudflare Worker(worker/worker.js) → GitHub Actions(telegram.yml)가
  바로 실행되고, 메시지가 TELEGRAM_UPDATE로 넘어온다 → process_update(). 답장까지 1~2분.
- 주기 확인: 웹훅을 끄면(td webhook off) telegram.yml이 5분마다 getUpdates로 새 메시지를 가져온다 → process_inbox().
  어디까지 처리했는지는 data/telegram.json의 offset에 남긴다. 웹훅이 켜져 있으면 텔레그램이 getUpdates를 막는다(409).

공통:
- 등록한 채팅(TELEGRAM_CHAT_ID)에서 온 메시지만 처리한다. 다른 사람이 봇에 링크를 보내 Gemini 한도를 쓰지 못하게.
- 이미 요약한 영상이면 저장된 요약을 다시 보낸다(Gemini 호출 없음).
"""
from __future__ import annotations

import html
import json
import logging
import time

from . import gemini
from .config import DATA_DIR, Config
from .notifier import WebhookActive, notify, telegram_text, telegram_updates
from .pipeline import build_article
from .state import ARTICLES_DIR, load_article, now_iso, save_article
from .youtube import fmt_ts, video_from_ref, video_ids_in, video_info

log = logging.getLogger(__name__)
INBOX_PATH = DATA_DIR / "telegram.json"
HELP = "유튜브 영상 링크를 보내면 요약해서 답장할게. 보통 5~15분 걸려."


def _load() -> dict:
    if INBOX_PATH.exists():
        return json.loads(INBOX_PATH.read_text(encoding="utf-8"))
    return {"offset": 0, "requests": []}


def _save(st: dict) -> None:
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    st["requests"] = st["requests"][-200:]  # 최근 요청 기록만 남긴다
    INBOX_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def process_update(cfg: Config, update: dict) -> int:
    """웹훅 수신기가 넘겨준 메시지 하나를 처리한다. 접수 답장은 수신기가 이미 보냈다."""
    msg = update.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(cfg.secrets["TELEGRAM_CHAT_ID"]):
        log.warning("등록되지 않은 채팅의 메시지는 무시: chat %s", msg.get("chat", {}).get("id"))
        return 0
    gemini.set_deadline(time.time() + cfg.inbox_budget_minutes * 60)
    return _handle(cfg, msg, {"requests": []}, ack=False)


def process_inbox(cfg: Config) -> int:
    """새 메시지를 처리하고 요약해 보낸 영상 수를 돌려준다."""
    chat_id = cfg.secrets["TELEGRAM_CHAT_ID"]
    st = _load()
    try:
        updates = telegram_updates(cfg, st["offset"])
    except WebhookActive:
        log.info("웹훅이 켜져 있어 주기 확인은 건너뜀 (메시지는 웹훅으로 바로 처리됨)")
        return 0
    if not updates:
        return 0
    deadline = time.time() + cfg.inbox_budget_minutes * 60
    gemini.set_deadline(deadline)
    sent = 0
    for u in updates:
        if time.time() > deadline:  # 남은 메시지는 offset을 올리지 않아 다음 실행이 이어서 처리한다
            log.info("시간 예산 소진, 남은 메시지 %d개는 다음 실행에", len(updates) - updates.index(u))
            break
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == str(chat_id):
            sent += _handle(cfg, msg, st)
        else:
            log.warning("등록되지 않은 채팅의 메시지는 무시: chat %s", msg.get("chat", {}).get("id"))
        st["offset"] = u["update_id"] + 1
        _save(st)  # 메시지마다 저장해, 중간에 끊겨도 처리한 것을 다시 처리하지 않게
    return sent


def _handle(cfg: Config, msg: dict, st: dict, ack: bool = True) -> int:
    text = msg.get("text") or msg.get("caption") or ""
    reply_to = msg.get("message_id")
    ids = video_ids_in(text)
    if not ids:
        if text.strip():
            telegram_text(cfg, HELP, reply_to)
        return 0
    if len(ids) > cfg.inbox_max_links:
        telegram_text(cfg, f"링크가 {len(ids)}개라 앞의 {cfg.inbox_max_links}개만 요약할게.", reply_to)
    sent = 0
    for vid in ids[: cfg.inbox_max_links]:
        try:
            if (ARTICLES_DIR / f"{vid}.json").exists():
                article = load_article(vid)
                log.info("이미 요약한 영상이라 저장된 요약을 보냄: %s", vid)
            else:
                article = _summarize(cfg, vid, reply_to, ack)
                if article is None:
                    continue
            notify(cfg, article, reply_to)
            st["requests"].append({"video_id": vid, "at": now_iso(), "source": article.get("source")})
            sent += 1
        except gemini.AllModelsUnavailable as e:
            log.warning("Gemini를 쓸 수 없음: %s", e)
            telegram_text(cfg, "지금은 Gemini 무료 한도가 바닥났거나 과부하야. 한도가 풀리는 오후 4시쯤 이후에 다시 보내줘.",
                          reply_to)
        except Exception as e:
            log.exception("텔레그램 요청 처리 실패 %s", vid)
            telegram_text(cfg, f"요약하지 못했어: {html.escape(type(e).__name__)}. 잠시 후 다시 보내줘.", reply_to)
    return sent


def _summarize(cfg: Config, vid: str, reply_to: int | None, ack: bool = True) -> dict | None:
    video = video_from_ref(vid)
    info = video_info(vid)
    if info.live_now:
        telegram_text(cfg, "지금 방송 중이거나 예정된 영상이야. 방송이 끝난 뒤에 다시 보내줘.", reply_to)
        return None
    if ack:  # 웹훅 방식은 수신기가 이미 "접수했어"를 보냈다
        length = f" ({fmt_ts(info.duration)})" if info.duration else ""
        telegram_text(cfg, f"요약 중: <b>{html.escape(video.title)}</b>{length}", reply_to)
    log.info("텔레그램 요청 요약 (길이 %s): %s", fmt_ts(info.duration) if info.duration else "모름", video.title)
    article = build_article(cfg, video, force=True, info=info)
    article["requested"] = now_iso()  # 텔레그램으로 요청한 요약 (구독 채널 영상이면 자동 알림을 다시 보내지 않는다)
    save_article(vid, article)
    return article
