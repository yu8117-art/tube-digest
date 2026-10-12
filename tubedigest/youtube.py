"""유튜브 쪽 입력 수집: 새 영상 목록(공식 API 또는 RSS), 채널 ID 해석, 쇼츠 판별, 썸네일, 영상 길이."""
from __future__ import annotations

import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
API = "https://www.googleapis.com/youtube/v3"
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


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "")


def _api_get(path: str, timeout: int = 20, **params) -> dict:
    r = requests.get(f"{API}/{path}", params={**params, "key": _api_key()}, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"YouTube API {path} {r.status_code}: {r.text[:200]}")
    return r.json()


def _iso_duration(s: str) -> int | None:
    """ISO 8601 기간(PT1H38M13S)을 초로. 라이브 중이면 P0D."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec or None


def fetch_channel_videos(channel_id: str, timeout: int = 20) -> list[Video]:
    """채널의 최근 영상 15개를 최신순으로 반환한다.

    YOUTUBE_API_KEY가 있으면 공식 API(업로드 재생목록, 호출당 1단위)를 쓰고, 없거나 실패하면 RSS를 쓴다.
    RSS는 2026-10-11부터 이틀 넘게 거의 모든 채널에 404/500을 주는 장애가 있었다(PC·Actions 모두 실측).
    """
    if _api_key():
        try:
            return _api_channel_videos(channel_id, timeout)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            log.warning("YouTube API 실패, RSS로 대체: %s", e)
    return _rss_channel_videos(channel_id, timeout)


def _api_channel_videos(channel_id: str, timeout: int = 20) -> list[Video]:
    data = _api_get("playlistItems", timeout, part="snippet,contentDetails", maxResults=15,
                    playlistId="UU" + channel_id[2:])  # 채널의 '업로드' 재생목록 ID는 UC… → UU…
    videos = []
    for it in data.get("items", []):
        sn, cd = it["snippet"], it["contentDetails"]
        published = cd.get("videoPublishedAt") or sn.get("publishedAt", "")
        videos.append(Video(cd["videoId"], channel_id, sn.get("channelTitle", ""), sn.get("title", ""),
                            published.replace("Z", "+00:00"), sn.get("description", "")))
    return sorted(videos, key=lambda v: v.published, reverse=True)


def _rss_channel_videos(channel_id: str, timeout: int = 20, attempts: int = 4) -> list[Video]:
    """채널 RSS(최근 15개)를 최신순으로 반환. API 키 불필요.

    유튜브 RSS는 정상 채널에도 무작위로 404/5xx를 몰아서 돌려주고, 몇 시간씩 대부분의 채널에 404를 주는
    장애도 있다(실측). 그래서 약 12초만 재시도하고, 실패한 채널은 다음 실행 회차에 맡긴다.
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


@dataclass
class VideoInfo:
    duration: int | None = None       # 초. 알 수 없으면 None
    live_content: bool | None = None  # 라이브 방송(다시보기 포함)이면 True
    live_now: bool = False            # 지금 방송 중이거나 방송 예정
    source: str = "none"              # 정보를 얻은 곳 (api / player / watch / none)


def video_info(video_id: str, timeout: int = 20) -> VideoInfo:
    """영상 길이와 라이브 여부. RSS에는 없어서 유튜브 player API → watch 페이지 순으로 시도한다.

    GitHub Actions(클라우드 IP)에서는 watch 페이지도 player API도 길이를 주지 않았다(실측). 그래서
    YOUTUBE_API_KEY가 있으면 공식 API(videos.list, 호출당 1단위)를 먼저 쓴다.
    """
    if _api_key():
        try:
            items = _api_get("videos", timeout, part="contentDetails,snippet,liveStreamingDetails",
                             id=video_id).get("items", [])
            if items:
                it = items[0]
                state = it["snippet"].get("liveBroadcastContent", "none")  # live / upcoming / none
                return VideoInfo(_iso_duration(it["contentDetails"].get("duration", "")),
                                 "liveStreamingDetails" in it, state in ("live", "upcoming"), "api")
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
            log.warning("YouTube API 영상 정보 실패: %s", e)
    try:
        r = requests.post(
            "https://www.youtube.com/youtubei/v1/player?prettyPrint=false", timeout=timeout,
            headers={"User-Agent": UA["User-Agent"], "Content-Type": "application/json"},
            json={"videoId": video_id,
                  "context": {"client": {"clientName": "WEB", "clientVersion": "2.20251001.00.00", "hl": "ko"}}},
        )
        d = r.json().get("videoDetails") or {}
        if d.get("lengthSeconds") is not None:
            return VideoInfo(int(d["lengthSeconds"]) or None, bool(d.get("isLiveContent")),
                             bool(d.get("isLive") or d.get("isUpcoming")), "player")
    except (requests.RequestException, ValueError):
        pass
    try:
        page = requests.get(f"https://www.youtube.com/watch?v={video_id}", headers=UA, timeout=timeout).text
    except requests.RequestException:
        return VideoInfo()
    m = re.search(r'"lengthSeconds":"(\d+)"', page)
    if not m:
        return VideoInfo()
    live = re.search(r'"isLiveContent":(true|false)', page)
    now = re.search(r'"isLive":true|"isUpcoming":true', page)
    return VideoInfo(int(m.group(1)) or None, live.group(1) == "true" if live else None, bool(now), "watch")


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
