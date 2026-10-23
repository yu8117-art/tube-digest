"""명령줄 진입점: python -m tubedigest <명령> (윈도우에서는 td.cmd 래퍼 사용)"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 시스템 python으로 실행돼도 프로젝트 가상환경(.venv)의 python으로 다시 실행한다.
# 아래 import들이 가상환경에만 설치된 패키지(google-genai 등)를 쓰므로 그 전에 처리해야 한다.
_VENV_PY = Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
if _VENV_PY.exists() and Path(sys.prefix).resolve() != _VENV_PY.parents[1].resolve():
    sys.exit(subprocess.run([str(_VENV_PY), "-m", "tubedigest", *sys.argv[1:]],
                            cwd=_VENV_PY.parents[2]).returncode)

import html
import re

from . import pipeline
from .config import ROOT, Channel, load_channels, load_config, save_channels
from .gitsync import git_publish, git_pull
from .notifier import notify, telegram_chat_ids, telegram_messages
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
        pipeline.run(load_config(), dry_run=args.dry_run, do_notify=not args.no_notify)


def cmd_notify(args) -> None:
    with RunLock():
        n = pipeline.notify_pending(load_config(), State.load())
    print(f"알림 {n}건 전송")


def _pull_channels() -> bool:
    """채널 목록을 고치기 전에 GitHub의 최신 상태를 받는다 (git_push를 쓰는 경우만)."""
    if not load_config().git_push:
        return False
    try:
        git_pull()
        return True
    except RuntimeError as e:
        print(f"경고: GitHub에서 최신 상태를 받지 못함 (로컬 목록으로 진행): {e}")
        return False


def _publish_channels(message: str) -> None:
    """바꾼 채널 목록을 GitHub에 올린다. 다음 Actions 실행부터 적용된다."""
    if not load_config().git_push:
        print("config.yaml의 git_push가 false라 GitHub에는 올리지 않음")
        return
    try:
        pushed = git_publish(message, paths=("channels.yaml",))
        print("GitHub 반영 완료 — 다음 실행(15분 안팎)부터 적용" if pushed else "GitHub에 올릴 변경 없음")
    except RuntimeError as e:
        print(f"GitHub 반영 실패: {e}\n  channels.yaml을 직접 커밋·push해야 Actions에 반영된다")


def cmd_add(args) -> None:
    _pull_channels()
    channels = load_channels()
    added = []
    for ref in args.refs:
        cid, name = resolve_channel_id(ref)
        if any(c.id == cid for c in channels):
            print(f"이미 등록됨: {name} ({cid})")
            continue
        channels.append(Channel(id=cid, name=name))
        added.append(name)
        print(f"추가: {name} ({cid})")
    if added:
        save_channels(channels)
        _publish_channels("채널 추가: " + ", ".join(added))


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def find_channels(channels: list[Channel], ref: str) -> tuple[list[Channel], list[Channel]]:
    """삭제할 채널을 찾는다. (찾은 채널, 후보) — 후보는 이름 일부가 여러 채널에 걸려 하나로 못 정한 경우.

    ID, 이름(앞의 @·대소문자·띄어쓰기 무시), @핸들·채널 URL(유튜브에서 ID 확인), 이름 일부(후보가 하나일 때) 순으로 본다.
    """
    q = ref.strip()
    key = _norm(q.lstrip("@"))
    hits = [c for c in channels if q == c.id or key == _norm(c.name)]
    if hits:
        return hits, []
    if q.startswith(("@", "http")):
        try:
            cid, _ = resolve_channel_id(q)
            hits = [c for c in channels if c.id == cid]
        except Exception:  # 핸들이 아니라 이름에 @를 붙인 경우 등
            hits = []
        if hits:
            return hits, []
    part = [c for c in channels if key and key in _norm(c.name)]
    return (part, []) if len(part) == 1 else ([], part)


def cmd_remove(args) -> None:
    _pull_channels()
    channels = load_channels()
    gone: list[Channel] = []
    for ref in args.refs:
        hits, candidates = find_channels(channels, ref)
        if hits:
            gone += [c for c in hits if c not in gone]
        elif candidates:
            print(f"'{ref}'에 해당하는 채널이 여러 개라 지우지 않음. 정확한 이름으로 다시 실행:")
            for c in candidates:
                print(f'  .\\td remove "{c.name}"')
        else:
            print(f"'{ref}'에 해당하는 채널 없음 (.\\td list로 확인)")
    if gone:
        save_channels([c for c in channels if c not in gone])
        print(f"삭제 {len(gone)}개: {', '.join(c.name for c in gone)}")
        _publish_channels("채널 삭제: " + ", ".join(c.name for c in gone))


def cmd_list(args) -> None:
    _pull_channels()
    for c in load_channels():
        print(f"{c.id}  {c.name}")


def cmd_summarize(args) -> None:
    """특정 영상 하나를 바로 요약해 화면에 보여준다. --send면 텔레그램으로 보내고 처리 기록에 남긴다."""
    cfg = load_config()
    video = video_from_ref(args.url)
    print(f"요약 중: [{video.channel_name}] {video.title}")
    article = pipeline.build_article(cfg, video, force=True)
    for msg in telegram_messages(article):  # 텔레그램에 보일 내용을 태그 없이 출력
        print(html.unescape(re.sub(r"<[^>]+>", "", msg)), end="\n\n")
    if not args.send:
        return
    save_article(video.video_id, article)
    notify(cfg, article)
    state = State.load()
    state.videos[video.video_id] = {"status": DONE, "published": video.published, "first_seen": now_iso(),
                                    "done_at": now_iso(), "source": article["source"], "notified": now_iso()}
    state.save()
    if cfg.git_push:
        git_publish(f"digest: 수동 요약 {video.video_id}")
    print(f"전송 완료 ({cfg.notify_provider})")


def cmd_test_notify(args) -> None:
    cfg = load_config()
    arts = load_all_articles()
    art = arts[0] if arts else json.loads((ROOT / "fixtures" / "sample_article.json").read_text(encoding="utf-8"))
    notify(cfg, art)
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
    p = argparse.ArgumentParser(prog="tubedigest", description="유튜브 새 영상 → Gemini 요약 → 텔레그램 알림")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="신규 영상 확인 후 요약·알림 (스케줄러가 호출)")
    r.add_argument("--dry-run", action="store_true", help="알림과 git push를 생략")
    r.add_argument("--no-notify", action="store_true", help="알림은 나중에 notify 명령으로")
    r.set_defaults(func=cmd_run)
    sub.add_parser("notify", help="아직 안 보낸 알림 전송").set_defaults(func=cmd_notify)
    a = sub.add_parser("add", help="채널 추가 (@핸들, 채널 URL, UC… ID) → GitHub 반영까지")
    a.add_argument("refs", nargs="+")
    a.set_defaults(func=cmd_add)
    rm = sub.add_parser("remove", help="채널 삭제 (이름, 이름 일부, @핸들, 채널 URL, UC… ID) → GitHub 반영까지")
    rm.add_argument("refs", nargs="+")
    rm.set_defaults(func=cmd_remove)
    sub.add_parser("list", help="등록 채널 보기 (GitHub 최신 상태 기준)").set_defaults(func=cmd_list)
    s = sub.add_parser("summarize", help="특정 영상 하나를 바로 요약해 화면에 출력")
    s.add_argument("url")
    s.add_argument("--send", action="store_true", help="텔레그램으로도 보내고 처리 기록에 남긴다")
    s.set_defaults(func=cmd_summarize)
    sub.add_parser("test-notify", help="폰 알림 테스트 (가장 최근 요약을 다시 보냄)").set_defaults(func=cmd_test_notify)
    sub.add_parser("telegram-chat-id", help="텔레그램 chat_id 확인").set_defaults(func=cmd_chat_id)
    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
