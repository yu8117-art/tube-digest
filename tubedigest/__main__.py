"""명령줄 진입점: python -m tubedigest <명령> (윈도우에서는 td.cmd 래퍼 사용)"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 시스템 python으로 실행돼도 프로젝트 가상환경(.venv)의 python으로 다시 실행한다.
# 아래 import들이 가상환경에만 설치된 패키지(anthropic 등)를 쓰므로 그 전에 처리해야 한다.
_VENV_PY = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parents[1].resolve():
    sys.exit(subprocess.run([str(_VENV_PY), "-m", "tubedigest", *sys.argv[1:]],
                            cwd=_VENV_PY.parents[2]).returncode)

from . import pipeline, publisher
from .config import ROOT, Channel, load_channels, load_config, save_channels
from .notifier import notify, telegram_chat_ids
from .state import DONE, RunLock, State, load_all_articles, now_iso, save_article
from .youtube import resolve_channel_id, video_from_ref


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [RotatingFileHandler(ROOT / "logs" / "digest.log", maxBytes=1_000_000, backupCount=3,
                                    encoding="utf-8")]
    if sys.stderr is not None:  # pythonw(작업 스케줄러)로 돌면 콘솔이 없다
        sys.stderr.reconfigure(errors="replace")
        sys.stdout.reconfigure(errors="replace")
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_run(args) -> None:
    with RunLock():
        published = pipeline.run(load_config(), dry_run=args.dry_run, do_notify=not args.no_notify)
    if out := os.environ.get("GITHUB_OUTPUT"):  # Actions에서 새 기사가 있을 때만 Pages 배포하도록
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"published={len(published)}\n")


def cmd_notify(args) -> None:
    with RunLock():
        n = pipeline.notify_pending(load_config(), State.load())
    print(f"알림 {n}건 전송")


def cmd_add(args) -> None:
    channels = load_channels()
    for ref in args.refs:
        cid, name = resolve_channel_id(ref)
        if any(c.id == cid for c in channels):
            print(f"이미 등록됨: {name} ({cid})")
            continue
        channels.append(Channel(id=cid, name=name))
        print(f"추가: {name} ({cid})")
    save_channels(channels)


def cmd_remove(args) -> None:
    channels = load_channels()
    keep = [c for c in channels if c.id != args.channel_id and c.name != args.channel_id]
    save_channels(keep)
    print(f"삭제 {len(channels) - len(keep)}개")


def cmd_list(args) -> None:
    for c in load_channels():
        print(f"{c.id}  {c.name}")


def cmd_summarize(args) -> None:
    """특정 영상 하나를 즉시 요약·발행 (테스트용, 자막 대기 없음)."""
    cfg = load_config()
    video = video_from_ref(args.url)
    print(f"요약 중: [{video.channel_name}] {video.title}")
    article = pipeline.build_article(cfg, video, force=True, skip_captions=args.video_analysis)
    save_article(video.video_id, article)
    path = publisher.render_article(cfg, article)
    publisher.render_index(cfg)
    url = publisher.article_url(cfg, video.video_id)
    state = State.load()
    state.videos[video.video_id] = {"status": DONE, "published": video.published, "first_seen": now_iso(),
                                    "done_at": now_iso(), "article_url": url, "notified": None}
    state.save()
    print(f"생성: {path}")
    if args.publish:
        if cfg.git_push:
            publisher.git_publish(f"digest: 수동 요약 {video.video_id}")
        pipeline.notify_pending(cfg, state)


def cmd_rebuild(args) -> None:
    n = publisher.rebuild_all(load_config())
    print(f"기사 {n}건 + 목록 페이지 재생성")


def cmd_preview(args) -> None:
    """샘플 기사로 디자인 미리보기 (preview/ 폴더, 배포 대상 아님)."""
    cfg = load_config()
    cfg.output_dir = ROOT / "preview"
    sample = json.loads((ROOT / "fixtures" / "sample_article.json").read_text(encoding="utf-8"))
    path = publisher.render_article(cfg, sample)
    out = cfg.output_dir / "index.html"
    out.write_text(publisher._env.get_template("index.html.j2").render(
        articles=[sample], updated=now_iso()), encoding="utf-8")
    print(f"미리보기: {path}\n목록: {out}")


def cmd_test_notify(args) -> None:
    cfg = load_config()
    arts = load_all_articles()
    art = arts[0] if arts else json.loads((ROOT / "fixtures" / "sample_article.json").read_text(encoding="utf-8"))
    url = publisher.article_url(cfg, art["video"]["video_id"]) if cfg.base_url else art["video"]["url"]
    notify(cfg, art, url)
    print(f"테스트 알림 전송 ({cfg.notify_provider})")


def cmd_chat_id(args) -> None:
    cfg = load_config()
    token = cfg.secrets["TELEGRAM_BOT_TOKEN"]
    if not token:
        sys.exit(".env에 TELEGRAM_BOT_TOKEN을 먼저 넣어라")
    ids = telegram_chat_ids(token)
    if not ids:
        print("받은 메시지가 없음. 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 실행해라.")
    for cid, name in ids:
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(prog="tubedigest", description="유튜브 새 영상 → 요약 기사 → 폰 알림")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="신규 영상 확인 후 요약·발행·알림 (스케줄러가 호출)")
    r.add_argument("--dry-run", action="store_true", help="git push와 알림을 생략")
    r.add_argument("--no-notify", action="store_true", help="알림은 나중에 notify 명령으로 (Actions용)")
    r.set_defaults(func=cmd_run)
    sub.add_parser("notify", help="아직 안 보낸 알림 전송").set_defaults(func=cmd_notify)
    a = sub.add_parser("add", help="채널 추가 (@핸들, 채널 URL, UC… ID)")
    a.add_argument("refs", nargs="+")
    a.set_defaults(func=cmd_add)
    rm = sub.add_parser("remove", help="채널 삭제 (ID 또는 이름)")
    rm.add_argument("channel_id")
    rm.set_defaults(func=cmd_remove)
    sub.add_parser("list", help="등록 채널 보기").set_defaults(func=cmd_list)
    s = sub.add_parser("summarize", help="특정 영상 하나를 바로 요약")
    s.add_argument("url")
    s.add_argument("--publish", action="store_true", help="git push 및 알림까지 진행")
    s.add_argument("--video-analysis", action="store_true",
                   help="자막을 건너뛰고 Gemini 영상 분석으로 요약 (GitHub Actions 동작 재현·비교용)")
    s.set_defaults(func=cmd_summarize)
    sub.add_parser("rebuild", help="템플릿 수정 후 모든 기사 HTML 재생성").set_defaults(func=cmd_rebuild)
    sub.add_parser("preview", help="샘플 기사로 디자인 미리보기").set_defaults(func=cmd_preview)
    sub.add_parser("test-notify", help="폰 알림 테스트").set_defaults(func=cmd_test_notify)
    sub.add_parser("telegram-chat-id", help="텔레그램 chat_id 확인").set_defaults(func=cmd_chat_id)
    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
