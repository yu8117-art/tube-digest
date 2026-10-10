"""자막을 받을 수 없을 때 Gemini가 유튜브 영상을 직접 보고 타임스탬프 노트를 만든다.

GitHub Actions 같은 클라우드 IP에서는 유튜브가 자막 요청을 막는다. Gemini API는 유튜브 URL을 넘기면
Google 쪽에서 영상을 읽으므로 이 차단과 상관이 없다. 여기서 만든 노트는 자막과 같은 형태(Transcript)로
요약 단계에 넘어가므로, 요약은 자막이 있을 때와 똑같이 사용자 프롬프트(prompts/summary_prompt.md)로 만든다.
"""
from __future__ import annotations

import logging
import os
import re
import time

from .config import Config
from .youtube import Transcript, Video, fmt_ts, video_duration

log = logging.getLogger(__name__)

TS_LINE = re.compile(r"^\s*[-*]?\s*\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*(.*)$")

PROMPT = """너는 유튜브 영상을 기록하는 속기사다. 이 영상을 처음부터 끝까지 보고 들으면서, 무엇을 말하고 보여주는지 시간 순서대로 빠짐없이 기록해라.

규칙
- 모든 줄은 [mm:ss] 또는 [h:mm:ss] 타임스탬프로 시작한다. 20~40초마다 새 줄을 쓴다.
- 화자가 말한 주장, 근거, 사례, 수치, 날짜, 인명·지명·기업명, 결론을 구체적으로 적는다. 뭉뚱그려 요약하지 마라.
- 화면에 나온 자막, 제목 문구, 표, 그래프의 내용은 줄 안에 [화면] 표시를 붙여 적는다.
- 영상에 없는 해석이나 평가를 덧붙이지 마라.
- 한국어로 쓴다. 외국어 영상이면 한국어로 옮겨 적는다.
- 타임스탬프 줄 외의 머리말, 맺음말, 설명은 쓰지 마라."""

_last_call = 0.0


class AnalysisError(Exception):
    pass


def available(cfg: Config) -> bool:
    return cfg.analysis_provider == "gemini" and bool(os.environ.get("GEMINI_API_KEY"))


def parse_notes(text: str, offset: int = 0) -> list[tuple[float, str]]:
    """'[mm:ss] 내용' 줄들을 (초, 내용)으로 바꾼다. 타임스탬프 없는 줄은 앞 줄에 이어 붙인다."""
    lines: list[tuple[float, str]] = []
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


def _ask(cfg: Config, client, video: Video, start: int | None, end: int | None,
         last: bool = True) -> list[tuple[float, str]]:
    global _last_call
    wait = cfg.analysis_pause_sec - (time.time() - _last_call)
    if _last_call and wait > 0:  # 무료 티어의 분당 토큰 한도를 넘지 않게 호출 사이 간격을 둔다
        time.sleep(wait)
    part: dict = {"type": "video", "uri": video.url}
    text = PROMPT
    if start is not None:
        # SDK는 오프셋을 "1800s"처럼 초+s 문자열로 받는다 (숫자를 넘기면 검증 오류)
        part["processing"] = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
        text += f"\n\n이 입력은 영상의 {fmt_ts(start)}부터 {fmt_ts(end)}까지 구간이다. 타임스탬프는 영상 전체 기준으로 적어라."
        if not last:  # 구간 끝을 영상의 끝으로 착각해 "영상이 마무리된다"고 적는 것을 막는다
            text += " 영상은 이 구간 뒤에도 계속되므로, 구간이 끝나는 지점을 영상의 끝이나 마무리로 적지 마라."
    try:
        res = client.interactions.create(model=cfg.analysis_model, input=[part, {"type": "text", "text": text}])
    finally:
        _last_call = time.time()
    lines = parse_notes(res.output_text or "", start or 0)
    if not lines:
        raise AnalysisError("타임스탬프 노트가 비어 있음")
    return lines


def analyze_video(cfg: Config, video: Video) -> Transcript:
    """영상을 Gemini로 분석해 타임스탬프 노트를 만든다. 긴 영상은 구간으로 나눠 분석한다."""
    from google import genai  # Gemini를 쓰지 않는 환경에서는 패키지가 없어도 되게

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    duration = video_duration(video.video_id)
    seg = cfg.analysis_segment_minutes * 60
    lines: list[tuple[float, str]] = []
    if duration and duration > seg:
        ranges = [(s, min(s + seg, duration)) for s in range(0, duration, seg)]
        try:
            for i, (s, e) in enumerate(ranges, 1):
                log.info("영상 분석 %d/%d (%s~%s): %s", i, len(ranges), fmt_ts(s), fmt_ts(e), video.title)
                lines.extend(_ask(cfg, client, video, s, e, last=i == len(ranges)))
        except Exception as e:  # 구간 지정이 유튜브 URL에 안 먹는 경우 등 → 전체를 한 번에 시도
            log.warning("구간 분석 실패, 전체 영상으로 재시도: %s", e)
            lines = []
    if not lines:
        log.info("영상 분석 (전체%s): %s", f", {fmt_ts(duration)}" if duration else "", video.title)
        lines = _ask(cfg, client, video, None, None)
    lines.sort(key=lambda x: x[0])
    return Transcript(language="ko", is_generated=True, lines=lines, kind="gemini")
