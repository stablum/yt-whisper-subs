"""yt-dlp metadata discovery for YouTube channels and individual videos.

Example: `YtDlpFeed(python).channel("https://youtube.com/@handle")`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse
from urllib.request import Request
from urllib.request import urlopen
from xml.etree import ElementTree

import yt_whisper_subs
from yt_whisper_subs import library_types as types
from yt_whisper_subs import proc
from yt_whisper_subs import youtube


def normalize_channel_url(value: str) -> str:
    """Normalize common handles and channel URLs to their videos tab.

    Example: `normalize_channel_url("@OpenAI")` ends in `/videos`.
    """

    raw = value.strip()
    if not raw:
        raise ValueError("enter a YouTube channel URL or @handle")
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}/videos"
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    host = parsed.netloc.casefold()
    host = host.removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com"}:
        raise ValueError("only YouTube channel URLs are supported")

    parts = [part for part in parsed.path.split("/") if part]
    is_channel = bool(parts) and (parts[0].startswith("@") or parts[0] in {"channel", "c", "user"})
    if not is_channel:
        raise ValueError("use a YouTube @handle, /channel/, /c/, or /user/ URL")
    if parts[-1] not in {"videos", "shorts", "streams"}:
        parts.append("videos")
    path = "/" + "/".join(parts)
    return urlunparse(("https", "www.youtube.com", path, "", "", ""))


def channel_tab_urls(url: str) -> list[str]:
    """Expand a canonical subscription into videos, Shorts, and streams tabs.

    Example: `channel_tab_urls(url)` returns three flat-playlist sources.
    """

    parsed = urlparse(normalize_channel_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[-1] in {"videos", "shorts", "streams"}:
        parts.pop()
    base = urlunparse(("https", "www.youtube.com", "/" + "/".join(parts), "", "", ""))
    return [f"{base}/{tab}" for tab in ("videos", "shorts", "streams")]


def timestamp_from_info(info: dict[str, Any]) -> int | None:
    """Read the best available YouTube publication timestamp.

    Example: `timestamp_from_info({"upload_date": "20260818"})`.
    """

    for key in ("timestamp", "release_timestamp"):
        value = info.get(key)
        if isinstance(value, int | float):
            return int(value)

    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        try:
            parsed = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return int(parsed.timestamp())
    return None


def _thumbnail_url(info: dict[str, Any]) -> str | None:
    """Pick the largest advertised thumbnail URL when one is available.

    Example: `_thumbnail_url(info)` feeds the optional detail panel link.
    """

    if thumbnail := info.get("thumbnail"):
        return str(thumbnail)
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        urls = [item.get("url") for item in thumbnails if isinstance(item, dict) and item.get("url")]
        return str(urls[-1]) if urls else None
    return None


def video_meta(info: dict[str, Any], parent: dict[str, Any] | None = None) -> types.VideoMeta:
    """Map yt-dlp JSON into the catalog's stable metadata model.

    Example: `video_meta(entry, channel_info)` parses flat entries too.
    """

    parent = parent or {}
    video_id = youtube.normalize_youtube_video_id(str(info.get("id") or ""))
    if not video_id:
        candidate_url = str(info.get("webpage_url") or info.get("url") or "")
        video_id = youtube.youtube_video_id(candidate_url)
    if not video_id:
        raise ValueError("yt-dlp entry did not contain a YouTube video ID")

    url = str(info.get("webpage_url") or info.get("url") or "")
    if not youtube.youtube_video_id(url):
        url = f"https://www.youtube.com/watch?v={video_id}"
    title = str(info.get("title") or video_id)
    channel = str(
        info.get("channel")
        or info.get("uploader")
        or parent.get("channel")
        or parent.get("uploader")
        or parent.get("title")
        or "Unknown channel"
    )
    channel_id = info.get("channel_id") or info.get("uploader_id")
    channel_id = str(channel_id) if channel_id else None
    duration = info.get("duration")
    duration = float(duration) if isinstance(duration, int | float) else None
    view_count = info.get("view_count")
    view_count = int(view_count) if isinstance(view_count, int | float) else None
    details = types.VideoDetails(
        duration=duration,
        view_count=view_count,
        description=str(info.get("description") or ""),
        thumbnail_url=_thumbnail_url(info),
        live_status=str(info["live_status"]) if info.get("live_status") else None,
    )
    return types.VideoMeta(
        identity=types.VideoIdentity(video_id, url, title),
        origin=types.VideoOrigin(channel, channel_id, timestamp_from_info(info)),
        details=details,
    )


def _rss_timestamp(text: str) -> int | None:
    """Parse an Atom publication timestamp into Unix seconds.

    Example: `_rss_timestamp("2026-08-18T12:30:00+00:00")`.
    """

    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(value.timestamp())


class YtDlpFeed:
    """Use the managed yt-dlp executable as the catalog discovery strategy.

    Example: `feed.video("https://youtu.be/...")` fetches full metadata.
    """

    def __init__(self, python_exe: Path, cookies_from_browser: str | None = None) -> None:
        self._python_exe = python_exe
        self._cookies = cookies_from_browser

    def with_cookies(self, cookies_from_browser: str | None) -> YtDlpFeed:
        """Return a client with updated browser-cookie routing.

        Example: `feed = feed.with_cookies("firefox")`.
        """

        return type(self)(self._python_exe, cookies_from_browser or None)

    def channel(self, url: str) -> types.ChannelSnapshot:
        """Fetch one complete flat channel listing without downloading media.

        Example: `snapshot = feed.channel(channel_url)`.
        """

        normalized_url = normalize_channel_url(url)
        tab_infos = []
        for idx, tab_url in enumerate(channel_tab_urls(normalized_url)):
            try:
                tab_infos.append(self._json(["--flat-playlist", "--dump-single-json", tab_url]))
            except RuntimeError:
                if idx == 0:
                    raise
        info = tab_infos[0]
        videos = []
        seen_ids: set[str] = set()
        for tab_info in tab_infos:
            for entry in tab_info.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    meta = video_meta(entry, tab_info)
                except ValueError:
                    continue
                if meta.identity.video_id not in seen_ids:
                    videos.append(meta)
                    seen_ids.add(meta.identity.video_id)

        youtube_id = info.get("channel_id") or info.get("uploader_id")
        youtube_id = str(youtube_id) if youtube_id else None
        if youtube_id:
            rss_dates = self._rss_dates(youtube_id)
            videos = [self._with_published_at(meta, rss_dates.get(meta.identity.video_id)) for meta in videos]

        title = str(info.get("channel") or info.get("uploader") or info.get("title") or normalized_url)
        if title.endswith(" - Videos"):
            title = title.removesuffix(" - Videos")
        return types.ChannelSnapshot(normalized_url, youtube_id, title, videos)

    def video(self, url: str) -> types.VideoMeta:
        """Fetch full metadata for one downloaded or selected video.

        Example: `meta = feed.video(video_url)`.
        """

        return self.video_info(url).meta

    def video_info(self, url: str) -> types.VideoInfo:
        """Fetch stable metadata plus the complete cleaned extractor document.

        Example: `info = feed.video_info(video_url)` for sidecar persistence.
        """

        info = self._json(["--dump-single-json", "--skip-download", "--no-playlist", url])
        return types.VideoInfo(video_meta(info), info)

    def _json(self, args: list[str]) -> dict[str, Any]:
        """Run yt-dlp quietly and parse its single JSON document.

        Example: `_json(["-J", url])` returns an extractor mapping.
        """

        cmd = [
            str(self._python_exe),
            "-m",
            "yt_dlp",
            "--ignore-errors",
            "--no-warnings",
            *args,
        ]
        if self._cookies:
            cmd[4:4] = ["--cookies-from-browser", self._cookies]
        result = subprocess.run(
            cmd,
            env=proc.child_process_env(),
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip().splitlines()
            message = detail[-1] if detail else f"yt-dlp exited with {result.returncode}"
            raise RuntimeError(message)
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("yt-dlp returned malformed metadata JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("yt-dlp returned unexpected metadata")
        return parsed

    @staticmethod
    def _rss_dates(channel_id: str) -> dict[str, int]:
        """Fetch exact publication times for YouTube's newest channel entries.

        Example: `_rss_dates("UC...")` enriches the latest flat results.
        """

        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        user_agent = f"Mozilla/5.0 yt-whisper-subs/{yt_whisper_subs.__version__}"
        request = Request(url, headers={"User-Agent": user_agent})
        try:
            with urlopen(request, timeout=20) as response:
                root = ElementTree.fromstring(response.read())
        except (OSError, ElementTree.ParseError):
            return {}
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
        }
        dates: dict[str, int] = {}
        for entry in root.findall("atom:entry", ns):
            video_id = entry.findtext("yt:videoId", default="", namespaces=ns)
            published = entry.findtext("atom:published", default="", namespaces=ns)
            if video_id and (timestamp := _rss_timestamp(published)) is not None:
                dates[video_id] = timestamp
        return dates

    @staticmethod
    def _with_published_at(meta: types.VideoMeta, published_at: int | None) -> types.VideoMeta:
        """Replace only missing flat-playlist publication data from RSS.

        Example: `_with_published_at(meta, timestamp)` keeps all other fields.
        """

        if meta.origin.published_at is not None or published_at is None:
            return meta
        origin = meta.origin._replace(published_at=published_at)
        return meta._replace(origin=origin)
