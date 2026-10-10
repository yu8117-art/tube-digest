"""유튜브 쪽 입력 수집: 채널 RSS, 채널 ID 해석, 쇼츠 판별, 썸네일, 자막."""
from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    PoTokenRequired,
    RequestBlocked,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

# 자막이 '없는' 게 아니라 '못 받는' 경우. 주로 클라우드 IP(GitHub Actions 등)에서 생긴다. IpBlocked는 RequestBlocked의 하위 클래스.
BLOCKED_ERRORS = (RequestBlocked, PoTokenRequired, YouTubeRequestFailed)

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
UA = {"User-Agent": "Mozilla/5.0 (tube-digest)", "Accept-Language": "ko-KR,ko;q=0.9"}
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass
class Video:
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    published: str
    description: str = ""

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class Transcript:
    language: str
    is_generated: bool
    lines: list[tuple[float, str]] = field(default_factory=list)  # (시작초, 텍스트)
    kind: str = "caption"  # caption: 유튜브 자막 / gemini: 자막 대신 영상을 직접 보고 만든 노트

    def as_prompt_text(self, bucket_sec: int = 20) -> str:
        """자막 조각을 bucket_sec 단위로 묶어 '[mm:ss] 텍스트' 줄로 만든다 (토큰 절약)."""
        out, cur_start, cur = [], None, []
        for start, text in self.lines:
            if cur_start is None:
                cur_start = start
            if start - cur_start >= bucket_sec and cur:
                out.append(f"[{fmt_ts(cur_start)}] {' '.join(cur)}")
                cur_start, cur = start, []
            cur.append(text.replace("\n", " ").strip())
        if cur:
            out.append(f"[{fmt_ts(cur_start)}] {' '.join(cur)}")
        return "\n".join(out)

    @property
    def duration(self) -> float:
        return self.lines[-1][0] if self.lines else 0.0


class TranscriptBlocked(Exception):
    """유튜브가 현재 IP의 자막 요청을 차단함 (주로 클라우드 IP)."""


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fetch_channel_videos(channel_id: str, timeout: int = 20, attempts: int = 6) -> list[Video]:
    """채널 RSS(최근 15개)를 최신순으로 반환. API 키 불필요.

    유튜브 RSS는 정상 채널에도 무작위로 404/5xx를 몰아서 돌려준다(실측: 3연속 404 후 200).
    그래서 최대 약 28초 동안 재시도하고, 그래도 실패하면 다음 실행 회차에 맡긴다.
    """
    url = RSS_URL.format(cid=channel_id)
    for i in range(attempts):
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.ok:
            break
        if r.status_code not in (404, 429, 500, 502, 503, 504) or i == attempts - 1:
            r.raise_for_status()
        time.sleep(min(2 * (i + 1), 8))
    root = ET.fromstring(r.content)
    channel_name = root.findtext("a:title", default="", namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        videos.append(
            Video(
                video_id=e.findtext("yt:videoId", namespaces=NS),
                channel_id=channel_id,
                channel_name=channel_name,
                title=e.findtext("a:title", default="", namespaces=NS),
                published=e.findtext("a:published", default="", namespaces=NS),
                description=e.findtext("media:group/media:description", default="", namespaces=NS),
            )
        )
    return videos


def resolve_channel_id(ref: str, timeout: int = 20) -> tuple[str, str]:
    """'UC…' ID, '@핸들', 채널 URL 중 무엇이든 받아 (channel_id, 채널명)을 반환."""
    ref = ref.strip()
    if re.fullmatch(r"UC[\w-]{22}", ref):
        url = f"https://www.youtube.com/channel/{ref}"
    elif ref.startswith("http"):
        url = ref
    else:
        url = f"https://www.youtube.com/@{ref.lstrip('@')}"
    page = requests.get(url, headers=UA, timeout=timeout)
    page.raise_for_status()
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page.text) \
        or re.search(r'"externalId":"(UC[\w-]{22})"', page.text)
    if not m:
        raise ValueError(f"채널 ID를 찾지 못함: {ref}")
    t = re.search(r'<meta property="og:title" content="([^"]+)"', page.text)
    return m.group(1), html.unescape(t.group(1)) if t else m.group(1)


def extract_video_id(ref: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{11}", ref):
        return ref
    raise ValueError(f"영상 ID를 찾지 못함: {ref}")


def video_from_ref(ref: str, timeout: int = 20) -> Video:
    """임의의 영상 URL/ID로 Video를 만든다 (수동 요약용). 채널 RSS에 있으면 설명란·게시일도 채운다."""
    vid = extract_video_id(ref)
    o = requests.get("https://www.youtube.com/oembed", headers=UA, timeout=timeout,
                     params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
    o.raise_for_status()
    meta = o.json()
    cid, _ = resolve_channel_id(meta["author_url"], timeout)
    for v in fetch_channel_videos(cid, timeout):
        if v.video_id == vid:
            return v
    from datetime import datetime, timezone
    return Video(vid, cid, meta["author_name"], meta["title"],
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))


def video_duration(video_id: str, timeout: int = 20) -> int | None:
    """영상 길이(초). RSS에는 길이가 없어서 watch 페이지에서 읽는다. 실패하면 None."""
    try:
        page = requests.get(f"https://www.youtube.com/watch?v={video_id}", headers=UA, timeout=timeout).text
    except requests.RequestException:
        return None
    m = re.search(r'"lengthSeconds":"(\d+)"', page)
    return int(m.group(1)) if m else None


def is_short(video_id: str, timeout: int = 10) -> bool:
    """/shorts/{id}가 200이면 쇼츠, 일반 영상은 watch로 리다이렉트된다."""
    try:
        r = requests.head(f"https://www.youtube.com/shorts/{video_id}", headers=UA,
                          allow_redirects=False, timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def thumbnail_url(video_id: str, timeout: int = 10) -> str:
    """maxres(1280x720)가 있으면 그것을, 없으면 hq(480x360)를 쓴다."""
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        url = f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
        try:
            r = requests.head(url, timeout=timeout)
            if r.status_code == 200:
                return url
        except requests.RequestException:
            pass
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def fetch_transcript(video_id: str, languages: list[str]) -> Transcript | None:
    """수동 자막 → 자동 생성 자막 순으로 찾는다. 자막이 (아직) 없으면 None.

    업로드 직후에는 자동 자막이 생성되지 않았을 수 있으므로 None은 '나중에 재시도'를 뜻한다.
    """
    api = YouTubeTranscriptApi()
    try:
        tl = api.list(video_id)
        try:
            t = tl.find_manually_created_transcript(languages)
        except CouldNotRetrieveTranscript:
            t = tl.find_generated_transcript(languages)
        fetched = t.fetch()
    except BLOCKED_ERRORS as e:
        raise TranscriptBlocked(f"{type(e).__name__}: {str(e).strip().splitlines()[0]}") from e
    except CouldNotRetrieveTranscript:
        return None
    return Transcript(
        language=t.language_code,
        is_generated=t.is_generated,
        lines=[(s.start, s.text) for s in fetched],
    )
