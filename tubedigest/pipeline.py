"""1회 실행 파이프라인: 신규 영상 감지 → Gemini 요약 → 텔레그램 알림 → 처리 기록 커밋."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from . import gemini
from .config import Config
from .gitsync import git_publish
from .notifier import NotifyError, notify
from .state import (BASELINE, DONE, FAILED, PENDING, SKIPPED_LIVE, SKIPPED_SHORT, State,
                    load_article, now_iso, save_article)
from .youtube import (Video, VideoInfo, fetch_channel_videos, fmt_ts, is_short,
                      thumbnail_url, video_info)

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


def build_article(cfg: Config, video: Video, rec: dict | None = None, force: bool = False,
                  info: VideoInfo | None = None) -> dict | None:
    """Gemini로 요약해 저장용 레코드를 만든다.

    영상 분석이 실패하면(무료 한도 초과, 응답 없음 등) gemini_wait_hours 동안은 None을 돌려 다음 회차에 재시도하고,
    그 뒤에는 제목·설명란·썸네일만으로 요약한다. force면 기다리지 않고 바로 그렇게 한다.
    max_video_minutes보다 긴 영상은 처음부터 제목·설명란·썸네일만으로 요약한다.
    """
    info = info or video_info(video.video_id)
    if info.duration and info.duration > cfg.max_video_minutes * 60:
        log.info("%d분 넘는 영상이라 영상 분석 없이 요약: %s", cfg.max_video_minutes, video.title)
        summary = gemini.summarize_metadata_only(cfg, video, info)
    else:
        try:
            summary = gemini.summarize(cfg, video, info)
        except Exception as e:
            log.warning("영상 분석 실패: %s: %s", type(e).__name__, str(e)[:300])
            if rec is not None:
                rec["last_error"] = f"{type(e).__name__}: {e}"[:300]
            if not force and _age_hours(video.published) < cfg.gemini_wait_hours:
                return None
            summary = gemini.summarize_metadata_only(cfg, video, info)
    return {
        "video": {**asdict(video), "url": video.url},
        "thumbnail": thumbnail_url(video.video_id),
        "source": summary.pop("_source"),
        "duration": summary.pop("_duration"),
        "summary": summary,
        "created_at": now_iso(),
    }


def process_pending(cfg: Config, state: State, started: float) -> list[str]:
    """대기 영상을 요약한다. 실행 시작(started) 후 run_budget_minutes 안에 끝낼 수 있는 것만 시작한다."""
    done = []
    if state.pending() and not gemini.available():
        log.error("GEMINI_API_KEY가 없어 요약을 건너뜀. 대기 영상 %d개는 그대로 둔다.", len(state.pending()))
        return done
    deadline = started + cfg.run_budget_minutes * 60
    # 최근에 시도했다 실패한 영상은 뒤로 보낸다. 한 영상이 매번 맨 앞에서 실패하며 나머지를 막지 않게.
    queue = sorted(state.pending(), key=lambda kv: (kv[1].get("last_attempt", ""), kv[1].get("published", "")))
    attempts = 0
    for vid, rec in queue:
        if attempts >= cfg.max_videos_per_run:  # 시도마다 Gemini 한도를 쓰므로 회차당 상한을 둔다
            break
        video = Video(**rec["video"])
        info = video_info(video.video_id)
        if info.live_now:
            log.info("방송 중/예정이라 다음에 처리: %s", video.title)
            continue
        if info.live_content and cfg.skip_live:
            rec["status"] = SKIPPED_LIVE
            rec.pop("video", None)
            log.info("라이브 다시보기라 건너뜀: %s", video.title)
            continue
        need = gemini.estimate_minutes(cfg, info.duration)
        if time.time() + need * 60 > deadline:
            log.info("이번 실행 시간 예산이 부족해 다음에 처리 (예상 %.0f분): %s", need, video.title)
            continue
        attempts += 1
        rec["last_attempt"] = now_iso()
        log.info("요약 시작 (길이 %s, 정보 출처 %s): %s",
                 fmt_ts(info.duration) if info.duration else "모름", info.source, video.title)
        try:
            article = build_article(cfg, video, rec, info=info)
        except Exception as e:  # 영상 하나의 실패가 나머지 처리를 막지 않게, 기록 후 다음 회차에 재시도
            rec["errors"] = rec.get("errors", 0) + 1
            rec["last_error"] = f"{type(e).__name__}: {e}"[:500]
            if rec["errors"] >= MAX_ERRORS:
                rec["status"] = FAILED
            log.error("요약 실패 (%d/%d) %s: %s", rec["errors"], MAX_ERRORS, video.title, e, exc_info=True)
            state.save()
            continue
        if article is None:
            rec["wait_checks"] = rec.get("wait_checks", 0) + 1
            log.info("영상 분석 재시도 대기 (%d회째): %s", rec["wait_checks"], video.title)
            continue
        save_article(vid, article)
        rec.update(status=DONE, done_at=now_iso(), source=article["source"])
        rec.pop("video", None)  # 요약 JSON에 보관되므로 상태 파일은 가볍게
        state.save()
        done.append(vid)
        log.info("요약 완료 (%s): %s", article["source"], video.title)
    return done


def send_notifications(cfg: Config, state: State) -> int:
    sent = 0
    for vid, rec in state.unnotified():
        try:
            notify(cfg, load_article(vid))
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
            git_publish("digest: 알림 완료 표시")
    return sent


def run(cfg: Config, dry_run: bool = False, do_notify: bool = True) -> list[str]:
    started = time.time()
    state = State.load()
    discover(cfg, state)
    state.save()

    done = process_pending(cfg, state, started)
    state.save()
    if dry_run:
        log.info("dry-run: 알림 / git push 생략 (요약 %d건)", len(done))
        return done
    if do_notify:
        send_notifications(cfg, state)
        state.save()
    if cfg.git_push:  # 요약 결과와 알림 완료 표시를 한 번에 커밋
        git_publish(f"digest: 요약 {len(done)}건" if done else "digest: 상태 갱신")
    return done
