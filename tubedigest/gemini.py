"""Gemini로 유튜브 영상을 직접 보고 요약한다. 썸네일 이미지 + 영상(유튜브 URL) → 고정 JSON 스키마.

자막을 쓰지 않는다. GitHub Actions 같은 클라우드 IP에서는 유튜브가 자막 요청을 막지만, Gemini는 유튜브 URL을
Google 쪽에서 읽으므로 영향이 없다. 요약 지시는 사용자 프롬프트(prompts/summary_prompt.md)를 그대로 쓴다.

- 짧은 영상(segment_minutes 이하): 영상을 넣고 한 번에 요약한다.
- 긴 영상: 구간별로 시간대 노트를 만든 뒤, 노트로 요약한다 (무료 티어의 요청당 토큰 한도 대응).
- 영상 분석이 계속 실패하면 호출하는 쪽에서 summarize_metadata_only()로 제목·설명란·썸네일만 요약한다.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time

import requests

from .config import PROMPT_PATH, Config
from .youtube import Video, VideoInfo, fmt_ts, thumbnail_url, video_info

log = logging.getLogger(__name__)

SYSTEM = """너는 유튜브 영상을 텔레그램 메시지로 읽기 좋게 정리하는 편집자다.
사용자 프롬프트의 지시를 그대로 따르고, 결과를 지정된 JSON 필드에 나눠 담는다.

문체 규칙
- 모든 문장은 반말 단정형으로 끝낸다. 예: "금리는 내년 상반기에 내린다.", "핵심은 공급 부족이다."
- 서술형 문단 대신 짧은 문장·항목으로 쓴다. 존댓말(~습니다/~요) 금지.
- 영상에 없는 내용을 지어내지 않는다. 화자의 주장은 주장으로 적는다.

필드 규칙
- headline: 요약 제목. 영상 제목을 베끼지 말고 핵심 결론이 드러나게 30자 안팎으로 쓴다.
- thumbnail_text: 썸네일 이미지에 적힌 문구를 그대로 옮긴다. 글자가 없으면 썸네일이 암시하는 질문을 한 줄로 적는다.
- thumbnail_answer: 썸네일이 던진 질문·주장에 대해 영상이 내놓은 답. 반드시 채운다. 영상이 답을 흐리면 흐린다고 적는다.
- conclusion: 결론 2~4문장.
- key_points: 핵심 요약 4~8개.
- timeline: 영상 재생 시간 기준 주요 구간 5~12개. seconds는 그 구간이 시작하는 재생 시점(초, 정수).
  영상을 볼 수 없으면 설명란의 챕터 타임스탬프를 쓰고, 그것도 없으면 빈 배열로 둔다.
- tags: 주제 키워드 3~6개 (# 없이).
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "thumbnail_text": {"type": "string"},
        "thumbnail_answer": {"type": "string"},
        "conclusion": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer"},
                    "title": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["seconds", "title", "points"],
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "thumbnail_text", "thumbnail_answer", "conclusion", "key_points", "timeline", "tags"],
}

NOTES_PROMPT = """이 영상을 처음부터 끝까지 보고 들으면서, 무엇을 말하고 보여주는지 시간 순서대로 빠짐없이 기록해라.

규칙
- 모든 줄은 [mm:ss] 또는 [h:mm:ss] 타임스탬프로 시작한다. 20~40초마다 새 줄을 쓴다.
- 화자가 말한 주장, 근거, 사례, 수치, 날짜, 인명·지명·기업명, 결론을 구체적으로 적는다. 뭉뚱그려 요약하지 마라.
- 화면에 나온 자막, 제목 문구, 표, 그래프의 내용은 줄 안에 [화면] 표시를 붙여 적는다.
- 영상에 없는 해석이나 평가를 덧붙이지 마라.
- 한국어로 쓴다. 외국어 영상이면 한국어로 옮겨 적는다.
- 타임스탬프 줄 외의 머리말, 맺음말, 설명은 쓰지 마라."""

TS_LINE = re.compile(r"^\s*[-*]?\s*\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*(.*)$")

_last_video_call = 0.0
_client_instance = None


class GeminiError(Exception):
    pass


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _client():
    # 클라이언트를 보관해 재사용한다. 임시 객체로 만들고 바로 호출하면 요청 전에 정리되며 연결이 닫힌다.
    global _client_instance
    if _client_instance is None:
        from google import genai
        _client_instance = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client_instance


def _create(cfg: Config, **kwargs):
    """Gemini 호출에 상한 시간을 둔다.

    SDK는 기본 제한 시간이 없고 실패하면 재시도까지 해서, 응답 없는 요청 하나가 실행 전체를 60분 동안 붙잡고
    강제 종료되는 일이 20시간 동안 반복됐다(2026-10-11~12). 상한을 넘기면 포기하고 다음 회차에 다시 시도한다.
    """
    limit = cfg.gemini_call_timeout_minutes * 60
    box: dict = {}

    def work():
        try:
            box["res"] = _client().interactions.create(timeout=limit, **kwargs)
        except BaseException as e:  # 스레드 밖으로 그대로 전달
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)  # 멈춘 호출이 프로세스 종료를 막지 않게 daemon
    t.start()
    t.join(limit + 30)
    if t.is_alive():
        raise GeminiError(f"응답 없음 ({cfg.gemini_call_timeout_minutes}분 초과)")
    if "err" in box:
        raise box["err"]
    return box["res"]


def estimate_minutes(cfg: Config, duration: int | None) -> float:
    """요약에 걸릴 시간 추정(분). 실행 시간 예산 안에 끝낼 수 있을 때만 시작하려고 쓴다."""
    if not duration:
        return cfg.gemini_call_timeout_minutes  # 길이를 모르면 통째로 보내므로 최악을 가정
    segments = math.ceil(duration / (cfg.gemini_segment_minutes * 60))
    if segments <= 1:
        return 3
    return segments * (3 + cfg.gemini_pause_sec / 60) + 2


def _pace(cfg: Config) -> None:
    """영상 호출 사이에 간격을 둔다. 무료 티어의 분당 토큰 한도를 넘지 않게."""
    global _last_video_call
    wait = cfg.gemini_pause_sec - (time.time() - _last_video_call)
    if _last_video_call and wait > 0:
        time.sleep(wait)
    _last_video_call = time.time()


def _thumbnail_part(url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return []
    return [{"type": "image", "data": base64.standard_b64encode(r.content).decode(), "mime_type": "image/jpeg"}]


def _video_part(video: Video, start: int | None = None, end: int | None = None) -> dict:
    part: dict = {"type": "video", "uri": video.url}
    if start is not None:
        # SDK는 오프셋을 "1800s"처럼 초+s 문자열로 받는다 (숫자를 넘기면 검증 오류)
        part["processing"] = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
    return part


def _meta_text(video: Video, duration: int | None) -> str:
    length = f"\n길이: {fmt_ts(duration)}" if duration else ""
    return (f"<video>\n제목: {video.title}\n채널: {video.channel_name}\n게시: {video.published}{length}\n"
            f"URL: {video.url}\n설명란:\n{video.description}\n</video>")


def _request(text: str) -> str:
    return f"<request>\n{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n</request>\n\n{text}"


def _summarize_call(cfg: Config, parts: list[dict]) -> dict:
    res = _create(
        cfg,
        model=cfg.gemini_model,
        system_instruction=SYSTEM,
        input=parts,
        response_format={"type": "text", "mime_type": "application/json", "schema": SCHEMA},
    )
    text = res.output_text
    if not text:
        raise GeminiError("빈 응답")
    data = json.loads(text)
    u = getattr(res, "usage", None)
    data["_usage"] = {"model": cfg.gemini_model,
                      "input_tokens": getattr(u, "total_input_tokens", None),
                      "output_tokens": getattr(u, "total_output_tokens", None)}
    return data


def parse_notes(text: str, offset: int = 0) -> list[tuple[int, str]]:
    """'[mm:ss] 내용' 줄들을 (초, 내용)으로 바꾼다. 타임스탬프 없는 줄은 앞 줄에 이어 붙인다."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        m = TS_LINE.match(raw)
        if m:
            h, mi, s, body = m.groups()
            lines.append((int(h or 0) * 3600 + int(mi) * 60 + int(s), body.strip()))
        elif lines and raw.strip():
            t, body = lines[-1]
            lines[-1] = (t, f"{body} {raw.strip()}".strip())
    lines = [(t, b) for t, b in lines if b]
    # 구간 분석인데 모델이 구간 시작을 0으로 보고 적었으면 영상 전체 기준으로 옮긴다.
    if offset and lines and max(t for t, _ in lines) < offset:
        lines = [(t + offset, b) for t, b in lines]
    return lines


def _segment_notes(cfg: Config, video: Video, start: int, end: int, last: bool) -> list[tuple[int, str]]:
    text = (f"{NOTES_PROMPT}\n\n이 입력은 영상의 {fmt_ts(start)}부터 {fmt_ts(end)}까지 구간이다. "
            "타임스탬프는 영상 전체 기준으로 적어라.")
    if not last:  # 구간 끝을 영상의 끝으로 착각해 "영상이 마무리된다"고 적는 것을 막는다
        text += " 영상은 이 구간 뒤에도 계속되므로, 구간이 끝나는 지점을 영상의 끝이나 마무리로 적지 마라."
    _pace(cfg)
    res = _create(cfg, model=cfg.gemini_model, input=[_video_part(video, start, end), {"type": "text", "text": text}])
    lines = parse_notes(res.output_text or "", start)
    if not lines:
        raise GeminiError(f"구간 노트가 비어 있음 ({fmt_ts(start)}~{fmt_ts(end)})")
    return lines


def summarize(cfg: Config, video: Video, info: VideoInfo | None = None) -> dict:
    """영상을 보고 요약한다. 반환값의 _source는 video(영상 직접) 또는 video_notes(긴 영상, 구간 노트 경유)."""
    duration = (info or video_info(video.video_id)).duration
    thumb = _thumbnail_part(thumbnail_url(video.video_id))
    seg = cfg.gemini_segment_minutes * 60
    if duration and duration > seg:
        ranges = [(s, min(s + seg, duration)) for s in range(0, duration, seg)]
        notes: list[tuple[int, str]] = []
        for i, (s, e) in enumerate(ranges, 1):
            log.info("영상 분석 %d/%d (%s~%s): %s", i, len(ranges), fmt_ts(s), fmt_ts(e), video.title)
            notes.extend(_segment_notes(cfg, video, s, e, last=i == len(ranges)))
        body = "\n".join(f"[{fmt_ts(t)}] {b}" for t, b in sorted(notes))
        text = (f"{_meta_text(video, duration)}\n\n<notes>\n아래는 이 영상을 처음부터 끝까지 보고 시간대별로 기록한 노트다. "
                f"영상을 본 것과 같은 근거로 쓰고, timeline의 seconds는 노트의 타임스탬프를 따른다.\n{body}\n</notes>\n\n"
                "첨부 이미지는 이 영상의 썸네일이다.")
        summary = _summarize_call(cfg, thumb + [{"type": "text", "text": _request(text)}])
        summary["_source"] = "video_notes"
    else:
        log.info("영상 분석%s: %s", f" ({fmt_ts(duration)})" if duration else "", video.title)
        _pace(cfg)
        text = f"{_meta_text(video, duration)}\n\n첨부 영상이 이 유튜브 영상이고, 첨부 이미지는 이 영상의 썸네일이다."
        summary = _summarize_call(cfg, [_video_part(video)] + thumb + [{"type": "text", "text": _request(text)}])
        summary["_source"] = "video"
    summary["_duration"] = duration
    return summary


def summarize_metadata_only(cfg: Config, video: Video, info: VideoInfo | None = None) -> dict:
    """영상 분석이 끝내 실패했거나 너무 긴 영상: 제목·설명란·썸네일만으로 요약한다."""
    duration = (info or video_info(video.video_id)).duration
    text = (f"{_meta_text(video, duration)}\n\n영상 자체는 분석하지 못했다. 제목·설명란·썸네일만으로 요약하고, "
            "그 한계를 conclusion에 한 줄로 밝힌다.\n첨부 이미지는 이 영상의 썸네일이다.")
    summary = _summarize_call(cfg, _thumbnail_part(thumbnail_url(video.video_id)) + [{"type": "text", "text": _request(text)}])
    summary["_source"] = "metadata"
    summary["_duration"] = duration
    return summary
