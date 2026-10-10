"""Claude API로 영상 요약을 만든다. 썸네일 이미지 + 타임스탬프 자막 → 고정 JSON 스키마."""
from __future__ import annotations

import base64
import json

import anthropic
import requests

from .config import PROMPT_PATH, Config
from .youtube import Transcript, Video, fmt_ts

SYSTEM = """너는 유튜브 영상을 모바일 매거진 기사로 정리하는 편집자다.
사용자 프롬프트의 지시를 그대로 따르고, 결과를 지정된 JSON 필드에 나눠 담는다.

문체 규칙
- 모든 문장은 반말 단정형으로 끝낸다. 예: "금리는 내년 상반기에 내린다.", "핵심은 공급 부족이다."
- 서술형 문단 대신 짧은 문장·항목으로 쓴다. 존댓말(~습니다/~요) 금지.
- 영상에 없는 내용을 지어내지 않는다. 화자의 주장은 주장으로 적는다.

필드 규칙
- headline: 기사 제목. 영상 제목을 베끼지 말고 핵심 결론이 드러나게 30자 안팎으로 쓴다.
- thumbnail_text: 썸네일 이미지에 적힌 문구를 그대로 옮긴다. 글자가 없으면 썸네일이 암시하는 질문을 한 줄로 적는다.
- thumbnail_answer: 썸네일이 던진 질문·주장에 대해 영상이 내놓은 답. 반드시 채운다. 영상이 답을 흐리면 흐린다고 적는다.
- conclusion: 결론 2~4문장.
- key_points: 핵심 요약 4~8개.
- timeline: 자막의 [mm:ss] 표시를 근거로 주요 구간 5~12개. seconds는 그 구간 시작 시점(초, 정수).
  자막이 없으면 설명란의 챕터 타임스탬프를 쓰고, 그것도 없으면 빈 배열로 둔다.
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
                "additionalProperties": False,
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "thumbnail_text", "thumbnail_answer", "conclusion",
                 "key_points", "timeline", "tags"],
    "additionalProperties": False,
}


class SummaryError(Exception):
    pass


def _thumbnail_block(url: str) -> dict | None:
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(r.content).decode()},
    }


def build_user_text(video: Video, transcript: Transcript | None) -> str:
    if transcript:
        if transcript.kind == "gemini":
            kind = "영상 분석 노트 (자막을 받을 수 없어 AI가 영상을 직접 보고 시간대별로 기록한 것. 자막처럼 그대로 근거로 쓴다)"
        else:
            kind = "자동 생성 자막" if transcript.is_generated else "업로드된 자막"
        body = (f"<transcript lang=\"{transcript.language}\" kind=\"{kind}\" "
                f"length=\"{fmt_ts(transcript.duration)}\">\n{transcript.as_prompt_text()}\n</transcript>")
    else:
        body = "<transcript>자막 없음. 제목·설명·썸네일만으로 요약하고, 그 한계를 conclusion에 한 줄로 밝힌다.</transcript>"
    return (
        f"<video>\n제목: {video.title}\n채널: {video.channel_name}\n게시: {video.published}\nURL: {video.url}\n"
        f"설명란:\n{video.description}\n</video>\n\n{body}\n\n"
        f"첨부 이미지는 이 영상의 썸네일이다.\n\n<request>\n{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n</request>"
    )


def summarize(cfg: Config, video: Video, thumb_url: str, transcript: Transcript | None) -> dict:
    client = anthropic.Anthropic()
    content = []
    if (img := _thumbnail_block(thumb_url)) is not None:
        content.append(img)
    content.append({"type": "text", "text": build_user_text(video, transcript)})

    # fallbacks="default": 안전 분류기가 드물게 거절하면 서버가 다른 모델로 자동 재시도한다.
    resp = client.beta.messages.create(
        model=cfg.model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise SummaryError(f"모델이 요약을 거절함: {getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise SummaryError("출력이 max_tokens에서 잘림")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise SummaryError(f"텍스트 응답 없음 (stop_reason={resp.stop_reason})")
    data = json.loads(text)
    data["_usage"] = {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                      "output_tokens": resp.usage.output_tokens}
    return data
