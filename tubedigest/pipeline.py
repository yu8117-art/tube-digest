"""1회 실행 파이프라인: 신규 영상 감지 → 자막/썸네일 수집 → 요약 → 기사 발행 → 알림."""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone

import anthropic

from . import publisher, video_analysis
from .config import Config
from .notifier import NotifyError, notify
from .state import (BASELINE, DONE, FAILED, PENDING, SKIPPED_SHORT, State,
                    load_article, now_iso, save_article)
from .summarizer import SummaryError, summarize
from .youtube import (TranscriptBlocked, Video, fetch_channel_videos,
                      fetch_transcript, is_short, thumbnail_url)

log = logging.getLogger(__name__)
MAX_ERRORS = 5


def _age_hours(published: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(published)).total_seconds() / 3600


def discover(cfg: Config, state: State) -> int:
    """RSS에서 처음 보는 영상을 상태에 등록한다. 채널을 처음 등록한 회차에는 기존 영상을 baseline으로 처리."""
    added = 0
    for ch in cfg.channels:
        try:
            videos = fetch_channel_videos(ch.id)
        except Exception as e:  # 한 채널 실패가 전체 실행을 막지 않게
            log.warning("RSS 조회 실패 %s(%s): %s", ch.name, ch.id, e)
            continue
        first = not state.channel_initialized(ch.id)
        for i, v in enumerate(videos):  # 최신순
            if v.video_id in state.videos:
                continue
            if first and i >= cfg.backfill_on_first_run:
                state.videos[v.video_id] = {"status": BASELINE, "published": v.published, "first_seen": now_iso()}
                continue
            status = SKIPPED_SHORT if cfg.skip_shorts and is_short(v.video_id) else PENDING
            state.videos[v.video_id] = {
                "status": status, "published": v.published, "first_seen": now_iso(),
                "video": asdict(v), "wait_checks": 0, "errors": 0,
            }
            if status == PENDING:
                added += 1
                log.info("새 영상: [%s] %s", v.channel_name, v.title)
        if first:
            state.mark_channel_initialized(ch.id)
            log.info("채널 등록: %s — 기존 영상 %d개는 건너뜀", ch.name or ch.id,
                     max(0, len(videos) - cfg.backfill_on_first_run))
    return added


def build_article(cfg: Config, video: Video, force: bool = False, rec: dict | None = None,
                  skip_captions: bool = False) -> dict | None:
    """요약 재료(자막 또는 영상 분석 노트)를 모아 기사를 만든다. 재료를 기다려야 하면 None(다음 회차에 재시도).

    1. 유튜브 자막을 받는다. 아직 생성 전이면 transcript_wait_hours까지 기다린다.
    2. 자막이 차단됐거나(GitHub Actions 등 클라우드 IP) 끝내 없으면 Gemini가 영상을 직접 보고 노트를 만든다.
       분석이 실패하면(무료 한도 초과 등) analysis_wait_hours까지 다음 회차에 다시 시도한다.
    3. 그래도 재료가 없으면 제목·설명란·썸네일만으로 요약한다.
    force면 기다리지 않는다. skip_captions면 자막을 건너뛰고 바로 영상 분석으로 간다(시험·비교용).
    rec를 주면 상태(ok / missing / blocked:원인, +gemini 등)를 기록한다.
    """
    status = "ok"
    try:
        tr = None if skip_captions else fetch_transcript(video.video_id, cfg.transcript_languages)
        if skip_captions:
            status = "skipped"
        elif tr is None:
            status = "missing"
    except TranscriptBlocked as e:
        log.warning("자막 요청이 차단됨: %s", e)
        tr, status = None, f"blocked:{str(e).split(':')[0]}"
    if rec is not None:
        rec["transcript_status"] = status
    if status == "missing" and not force and _age_hours(video.published) < cfg.transcript_wait_hours:
        return None

    if tr is None and video_analysis.available(cfg):
        try:
            tr = video_analysis.analyze_video(cfg, video)
            status += "+gemini"
        except Exception as e:
            log.warning("영상 분석 실패: %s: %s", type(e).__name__, str(e)[:300])
            status += "+gemini_failed"
            if rec is not None:
                rec["transcript_status"] = status
            if not force and _age_hours(video.published) < cfg.analysis_wait_hours:
                return None
        if rec is not None:
            rec["transcript_status"] = status

    thumb = thumbnail_url(video.video_id)
    summary = summarize(cfg, video, thumb, tr)
    if tr is None:
        source = "none"
    elif tr.kind == "gemini":
        source = "gemini"
    else:
        source = "generated" if tr.is_generated else "manual"
    return {
        "video": {**asdict(video), "url": video.url},
        "thumbnail": thumb,
        "thumbnail_small": f"https://i.ytimg.com/vi/{video.video_id}/mqdefault.jpg",
        "transcript": {
            "source": source,
            "language": tr.language if tr else None,
            "duration": int(tr.duration) if tr else 0,
        },
        "summary": summary,
        "created_at": now_iso(),
    }


def process_pending(cfg: Config, state: State) -> list[str]:
    published = []
    if state.pending() and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.error("ANTHROPIC_API_KEY가 없어 요약을 건너뜀 (.env 확인). 대기 영상 %d개는 그대로 둔다.", len(state.pending()))
        return published
    attempts = 0  # 요약까지 간 횟수만 센다. 자막 대기로 넘긴 영상은 상한을 차지하지 않는다.
    for vid, rec in state.pending():
        if attempts >= cfg.max_videos_per_run:
            break
        video = Video(**rec["video"])
        try:
            article = build_article(cfg, video, rec=rec)
        except Exception as e:  # 영상 하나의 실패가 나머지 처리를 막지 않게, 기록 후 다음 회차에 재시도
            attempts += 1
            rec["errors"] += 1
            rec["last_error"] = f"{type(e).__name__}: {e}"[:500]
            if rec["errors"] >= MAX_ERRORS:
                rec["status"] = FAILED
            log.error("요약 실패 (%d/%d) %s: %s", rec["errors"], MAX_ERRORS, video.title, e,
                      exc_info=not isinstance(e, (SummaryError, anthropic.APIError)))
            state.save()
            continue
        if article is None:
            rec["wait_checks"] += 1
            log.info("자막 대기 중 (%d회째, %s): %s", rec["wait_checks"], rec["transcript_status"], video.title)
            continue
        attempts += 1
        save_article(vid, article)
        publisher.render_article(cfg, article)
        rec.update(status=DONE, done_at=now_iso(), article_url=publisher.article_url(cfg, vid))
        rec.pop("video", None)  # 기사 JSON에 보관되므로 상태 파일은 가볍게
        state.save()
        published.append(vid)
        log.info("기사 발행: %s", rec["article_url"])
    return published


def send_notifications(cfg: Config, state: State) -> int:
    sent = 0
    for vid, rec in state.unnotified():
        url = rec["article_url"]
        if cfg.git_push:
            publisher.wait_until_live(url, cfg.wait_until_live_sec)
        try:
            notify(cfg, load_article(vid), url)
        except (NotifyError, OSError) as e:
            log.error("알림 실패 %s: %s", vid, e)
            continue
        rec["notified"] = now_iso()
        sent += 1
    return sent


def notify_pending(cfg: Config, state: State) -> int:
    sent = send_notifications(cfg, state)
    if sent:
        state.save()
        if cfg.git_push:  # 알림 완료 표시를 저장해야 Actions 환경에서 중복 알림이 안 생긴다
            publisher.git_publish("digest: 알림 완료 표시")
    return sent


def run(cfg: Config, dry_run: bool = False, do_notify: bool = True) -> list[str]:
    state = State.load()
    discover(cfg, state)
    state.save()

    published = process_pending(cfg, state)
    state.save()
    if published or not (cfg.output_dir / "index.html").exists():
        publisher.render_index(cfg)

    if dry_run:
        log.info("dry-run: git push / 알림 생략 (발행 %d건)", len(published))
        return published
    if cfg.git_push:
        publisher.git_publish(f"digest: 새 기사 {len(published)}건" if published else "digest: 상태 갱신")
    if do_notify:
        notify_pending(cfg, state)
    return published
