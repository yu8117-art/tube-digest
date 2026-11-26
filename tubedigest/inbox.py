"""텔레그램으로 받은 유튜브 링크를 요약해서 답장한다. 두 가지 방식이 있다.

- 웹훅(기본): 봇에 메시지가 오면 텔레그램 → Cloudflare Worker(worker/worker.js) → GitHub Actions(telegram.yml)가
  바로 실행되고, 메시지가 TELEGRAM_UPDATE로 넘어온다 → process_update(). 답장까지 1~2분.
- 주기 확인: 웹훅을 끄면(td webhook off) telegram.yml이 5분마다 getUpdates로 새 메시지를 가져온다 → process_inbox().
  어디까지 처리했는지는 data/telegram.json의 offset에 남긴다. 웹훅이 켜져 있으면 텔레그램이 getUpdates를 막는다(409).

공통:
- 등록한 채팅(TELEGRAM_CHAT_ID)에서 온 메시지만 처리한다. 다른 사람이 봇에 링크를 보내 Gemini 한도를 쓰지 못하게.
- 이미 요약한 영상이면 저장된 요약을 다시 보낸다(Gemini 호출 없음).
- Gemini가 한도 초과·과부하면 대기열(data/queue/{영상ID}.json)에 넣고 그렇게 답장한다. 15분마다 도는 구독 확인
  (pipeline.run)이 대기열을 요청 순서대로 먼저 처리하고, 원래 링크 메시지에 요약을 답장으로 붙인다.
  요청마다 파일을 따로 두는 건, 동시에 도는 웹훅 실행과 구독 확인이 같은 파일을 고쳐 충돌하지 않게 하려는 것이다.
"""
from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone

from . import gemini
from .config import DATA_DIR, Config
from .notifier import WebhookActive, notify, telegram_text, telegram_updates
from .pipeline import build_article
from .state import ARTICLES_DIR, load_article, now_iso, save_article
from .youtube import fmt_ts, video_from_ref, video_ids_in, video_info

log = logging.getLogger(__name__)
INBOX_PATH = DATA_DIR / "telegram.json"
QUEUE_DIR = DATA_DIR / "queue"
QUEUE_MAX_ATTEMPTS = 3   # 한도·과부하가 아닌 오류가 이만큼 나면 포기하고 알린다
QUEUE_MAX_DAYS = 7       # 이만큼 지나도 처리 못 하면 포기하고 알린다
HELP = "유튜브 영상 링크를 보내면 요약해서 답장할게. 보통 5~15분 걸려."


def queued() -> list[dict]:
    """대기열을 요청 순서대로."""
    if not QUEUE_DIR.exists():
        return []
    items = [json.loads(p.read_text(encoding="utf-8")) for p in QUEUE_DIR.glob("*.json")]
    return sorted(items, key=lambda q: q["requested_at"])


def _queue_path(vid: str):
    return QUEUE_DIR / f"{vid}.json"


def enqueue(vid: str, reply_to: int | None) -> tuple[int, bool]:
    """대기열에 넣고 (순번, 새로 넣었는지)를 돌려준다. 이미 있으면 처음 요청한 자리를 지킨다."""
    new = not _queue_path(vid).exists()
    if new:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        _queue_path(vid).write_text(json.dumps(
            {"video_id": vid, "reply_to": reply_to, "requested_at": now_iso(), "attempts": 0},
            ensure_ascii=False, indent=1), encoding="utf-8")
    pos = [q["video_id"] for q in queued()].index(vid) + 1
    return pos, new


def dequeue(vid: str) -> None:
    _queue_path(vid).unlink(missing_ok=True)


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
            dequeue(vid)  # 대기열에 있던 영상을 다시 보내 바로 처리된 경우
            st["requests"].append({"video_id": vid, "at": now_iso(), "source": article.get("source")})
            sent += 1
        except gemini.AllModelsUnavailable as e:
            log.warning("Gemini를 쓸 수 없어 대기열에 넣음: %s (%s)", vid, e)
            pos, new = enqueue(vid, reply_to)
            telegram_text(cfg, (f"지금은 Gemini 한도 초과나 과부하라 대기열에 넣었어 (대기 {pos}번째). "
                                if new else f"이미 대기열에 있어 (대기 {pos}번째). ")
                          + "처리되면 자동으로 요약해서 답장할게. 다시 보낼 필요 없어.", reply_to)
        except Exception as e:
            log.exception("텔레그램 요청 처리 실패 %s", vid)
            telegram_text(cfg, f"요약하지 못했어: {html.escape(type(e).__name__)}. 잠시 후 다시 보내줘.", reply_to)
    return sent


def process_queue(cfg: Config, deadline: float) -> int:
    """대기열의 요청을 순서대로 요약해 원래 메시지에 답장한다 (구독 확인 실행이 먼저 호출). 보낸 수를 돌려준다.

    Gemini가 아직 막혀 있으면 남은 요청은 그대로 두고 멈춘다. 다음 회차(15분 뒤)에 다시 시도한다.
    """
    sent = 0
    for item in queued():
        vid, reply_to = item["video_id"], item.get("reply_to")
        link = f"https://youtu.be/{vid}"
        if time.time() > deadline:
            log.info("시간 예산 소진, 대기열 %d개는 다음 회차에", len(queued()))
            break
        if (datetime.now(timezone.utc) - datetime.fromisoformat(item["requested_at"])).days >= QUEUE_MAX_DAYS:
            telegram_text(cfg, f"{QUEUE_MAX_DAYS}일 동안 요약하지 못해 대기열에서 뺐어: {link}", reply_to)
            dequeue(vid)
            continue
        try:
            if (ARTICLES_DIR / f"{vid}.json").exists():  # 그사이 구독 확인 등으로 이미 요약됨
                article = load_article(vid)
            else:
                article = _summarize(cfg, vid, reply_to, ack=False)
                if article is None:  # 방송 중/예정 → 그렇게 안내했으니 대기열에서 뺀다
                    dequeue(vid)
                    continue
            notify(cfg, article, reply_to)
            dequeue(vid)
            sent += 1
            log.info("대기열 요청 처리 완료: %s", vid)
        except gemini.AllModelsUnavailable as e:
            log.warning("Gemini가 아직 막혀 있어 대기열 처리를 멈춤 (%d개 남음): %s", len(queued()), e)
            break
        except Exception as e:
            item["attempts"] = item.get("attempts", 0) + 1
            item["last_error"] = f"{type(e).__name__}: {e}"[:300]
            log.exception("대기열 요청 처리 실패 (%d/%d) %s", item["attempts"], QUEUE_MAX_ATTEMPTS, vid)
            if item["attempts"] >= QUEUE_MAX_ATTEMPTS:
                telegram_text(cfg, f"요약하지 못해 대기열에서 뺐어 ({html.escape(type(e).__name__)}). "
                                   f"링크를 다시 보내면 다시 시도할게: {link}", reply_to)
                dequeue(vid)
            else:
                _queue_path(vid).write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
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
