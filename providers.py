"""Anonymous extraction and a small shared vocabulary for public web videos."""

import functools
import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request

import yt_dlp
import requests
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.extractor import gen_extractor_classes

LOG = logging.getLogger(__name__)
SOURCES = {
    "youtube": "YouTube",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "twitch": "Twitch",
    "peertube": "PeerTube",
    "bilibili": "Bilibili",
    "tiktok": "TikTok",
}


class Unavailable(RuntimeError):
    """An extractor positively reported an offline or unavailable item."""


class ExtractLog:
    def debug(self, message):
        LOG.debug("extractor: %s", message)

    def warning(self, message):
        LOG.debug("extractor: %s", message)

    def error(self, message):
        # The caller logs a source-specific failure once, rather than twice.
        LOG.debug("extractor: %s", message)


def provider(extractor):
    """Use yt-dlp's extractor identity, not a growing list of website hostnames."""
    name = (extractor or "generic").lower()
    return next((key for key in SOURCES if name.startswith(key)), name.split(":")[0])


@functools.lru_cache(maxsize=1)
def extractors():
    return [cls for cls in gen_extractor_classes() if cls.ie_key() != "Generic"]


def detect(url):
    """Offline URL classification; extraction's actual identity remains authoritative."""
    for cls in extractors():
        if cls.suitable(url):
            return provider(cls.ie_key())
    return "generic"


def source_lines(path):
    """Preserve the existing 'URL | optional collection name' text-file interface."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            url, _, alias = line.partition("|")
            if urlsplit(url.strip()).scheme not in ("http", "https"):
                logging.getLogger(__name__).warning(
                    "Skipping a source entry: expected an HTTP(S) URL"
                )
                continue
            yield url.strip(), alias.strip()


def streamlink_live(url):
    """Resolve public live plugins with anonymous, memory-only Streamlink state."""
    import streamlink
    from streamlink.cache import Cache

    session = streamlink.Streamlink()
    session.set_option("http-timeout", 20)
    _, plugin_class, resolved = session.resolve_url(url)
    plugin = plugin_class(session, resolved)
    plugin.cache = Cache("rex-youtube-disabled", disabled=True)
    source = provider(plugin_class.__module__.rsplit(".", 1)[-1])
    title, author, category, broadcast = (
        plugin.get_title(),
        plugin.get_author(),
        plugin.get_category(),
        plugin.get_id(),
    )
    streams = plugin.streams()
    if not streams:
        if source == "twitch" and not broadcast:
            raise Unavailable("Source is currently offline")
        raise RuntimeError("Public live formats are temporarily unavailable")
    author = author or urlsplit(url).path.strip("/") or urlsplit(url).hostname
    formats = []
    for name, stream in streams.items():
        quality = re.match(r"(\d+)p", name)
        if (not quality and name != "best") or not hasattr(stream, "url"):
            continue
        height = int(quality[1]) if quality else None
        formats.append(
            {
                "format_id": name,
                "url": stream.url,
                "protocol": "m3u8_native",
                "vcodec": "avc1.640028",
                "acodec": "mp4a.40.2",
                "height": height,
                "width": round(height * 16 / 9) if height else None,
                "http_headers": dict(
                    session.http.prepare_request(
                        requests.Request("GET", stream.url)
                    ).headers
                ),
            }
        )
    return {
        "id": str(broadcast or hashlib.sha256(url.encode()).hexdigest()[:24]),
        "extractor_key": source + ":Streamlink",
        "webpage_url": url,
        "is_live": True,
        "title": title or author,
        "channel_id": author,
        "channel": author,
        "description": category or "",
        "formats": formats,
    }


def extract(url, *, flat=False, limit=None):
    """Use fresh anonymous extractor state; no cookie file, account or disk cache."""
    if not flat and re.fullmatch(r"https?://(?:www\.)?twitch.tv/[\w-]+/?", url):
        return streamlink_live(url)
    if detect(url) == "generic":
        import streamlink

        try:
            streamlink.Streamlink().resolve_url(url)
        except streamlink.NoPluginError:
            pass
        else:
            return streamlink_live(url)
    # yt-dlp returns its own canonical collection URLs; accept PeerTube's long form too.
    url = (
        re.sub(r"(/)video-channels/", r"\1c/", url)
        if "/video-channels/" in url
        else url
    )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cachedir": False,
        "logger": ExtractLog(),
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "js_runtimes": {"node": {}},
        "extract_flat": "in_playlist" if flat else False,
    }
    if detect(url) in ("bilibili", "tiktok"):
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    if limit:
        opts["playlistend"] = limit
    # Vimeo's public embed player works anonymously; its web API requires login.
    public_vimeo = re.fullmatch(r"https?://(?:www\.)?vimeo.com/(\d+)/?", url)
    if public_vimeo:
        opts["http_headers"] = {"Referer": url}
        url = "https://player.vimeo.com/video/" + public_vimeo[1]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                # The anonymous Vimeo embed omits dates/creator URLs. Public oEmbed
                # supplies those canonical fields without a login or a page scraper.
                if (
                    not flat
                    and provider(info.get("extractor_key")) == "vimeo"
                    and not info.get("upload_date")
                ):
                    try:
                        response = requests.get(
                            "https://vimeo.com/api/oembed.json",
                            params={"url": "https://vimeo.com/" + str(info["id"])},
                            timeout=15,
                        )
                        response.raise_for_status()
                        public = response.json()
                        info["upload_date"] = (public.get("upload_date") or "")[
                            :10
                        ].replace("-", "")
                        info["uploader_url"] = public.get("author_url") or info.get(
                            "uploader_url"
                        )
                        info["uploader_id"] = (info.get("uploader_url") or "").rstrip(
                            "/"
                        ).rsplit("/", 1)[-1] or info.get("uploader_id")
                    except (requests.RequestException, ValueError):
                        LOG.debug(
                            "Vimeo public metadata unavailable; keeping the embed metadata"
                        )
                for fmt in info.get("formats", []):
                    request = Request(fmt["url"])
                    ydl.cookiejar.add_cookie_header(request)
                    if request.get_header("Cookie"):
                        fmt["http_headers"] = {
                            **fmt.get("http_headers", {}),
                            "Cookie": request.get_header("Cookie"),
                        }
            return info
    except yt_dlp.utils.DownloadError as error:
        # A network/API failure must never be mistaken for a live -> offline transition.
        if re.search(
            r"currently offline|is offline|not currently live|not broadcasting|livestream has ended|not live",
            str(error),
            re.I,
        ):
            raise Unavailable("Source is currently offline") from error
        raise


def entry_url(entry):
    url = entry.get("webpage_url") or entry.get("url") or ""
    if urlsplit(url).scheme in ("http", "https"):
        return url
    if provider(entry.get("ie_key")) == "youtube":
        return "https://www.youtube.com/watch?v=" + str(entry["id"])
    raise ValueError("Extractor did not provide a playable webpage URL")


@functools.lru_cache(maxsize=128)
def creator_metadata(source, ident):
    """Fill creator biographies/avatars from public APIs that need no account."""
    try:
        if source == "dailymotion":
            response = requests.get(
                "https://api.dailymotion.com/user/" + str(ident),
                params={"fields": "description,avatar_720_url,created_time"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return {
                "description": data.get("description"),
                "art": data.get("avatar_720_url"),
                "year": (
                    datetime.fromtimestamp(data["created_time"], timezone.utc).year
                    if data.get("created_time")
                    else None
                ),
            }
        if source == "vimeo":
            response = requests.get(
                "https://vimeo.com/api/v2/" + str(ident) + "/info.json", timeout=15
            )
            response.raise_for_status()
            data = response.json()
            return {
                "description": data.get("bio"),
                "art": data.get("portrait_huge"),
                "year": (data.get("created_on") or "")[:4] or None,
            }
        if source == "twitch":
            from yt_dlp.extractor.twitch import TwitchBaseIE

            response = requests.post(
                "https://gql.twitch.tv/gql",
                headers={
                    "Client-ID": TwitchBaseIE(
                        yt_dlp.YoutubeDL({"quiet": True, "cachedir": False})
                    )._CLIENT_ID
                },
                json={
                    "query": "query Creator($login:String!){user(login:$login){description profileImageURL(width:300) createdAt}}",
                    "variables": {"login": str(ident)},
                },
                timeout=15,
            )
            response.raise_for_status()
            data = response.json().get("data", {}).get("user") or {}
            return {
                "description": data.get("description"),
                "art": data.get("profileImageURL"),
                "year": (data.get("createdAt") or "")[:4] or None,
            }
    except (requests.RequestException, ValueError, KeyError, TypeError):
        LOG.debug("Public creator metadata unavailable for %s", source)
    return {}


def enumerate_source(url, alias="", limit=1000):
    """Yield webpage references, leaving expensive per-video extraction to the poller."""
    detected = detect(url)
    if (
        detected == "youtube"
        and "/@" in url
        and not re.search(r"/(videos|streams|shorts|live)/?$", url)
    ):
        url = url.rstrip("/") + "/videos"
    if detected == "twitch" and re.fullmatch(
        r"https?://(?:www\.)?twitch.tv/[\w-]+/videos/?", url
    ):
        url = url.rstrip("/") + "?filter=archives&sort=time"
    info = extract(url, flat=True, limit=limit)
    if not info:
        return
    collection = "entries" in info
    entries = list(info.get("entries") or []) if collection else [info]
    # Channel extractors normally enumerate newest first. The persisted dates ultimately
    # determine numbering; this order means a first scan backfills oldest uploads first.
    if collection:
        entries.reverse()
    context = {
        "collection": collection,
        "alias": alias,
        "name": alias
        or info.get("channel")
        or info.get("uploader")
        or info.get("title"),
        "creator_id": info.get("channel_id")
        or info.get("uploader_id")
        or info.get("id"),
        "art": channel_art(info),
        "description": info.get("description") or "",
        "provider": provider(
            info.get("extractor_key") or info.get("extractor") or detected
        ),
    }
    if collection and entries:
        details = creator_metadata(context["provider"], context["creator_id"])
        context["description"] = (
            context["description"] or details.get("description") or ""
        )
        context["art"] = context["art"] or details.get("art")
        context["year"] = details.get("year")
    for entry in entries:
        if entry:
            yield entry_url(entry), context, entry


def channel_art(info):
    square = [
        t
        for t in info.get("thumbnails", [])
        if t.get("width") and t.get("height") and abs(t["width"] - t["height"]) < 10
    ]
    return max(square, key=lambda t: t["width"])["url"] if square else None


def normalize(info, context=None):
    """Persist only canonical metadata: no formats, signed URLs or transient cookies."""
    context = context or {}
    source = provider(
        info.get("extractor_key") or info.get("extractor") or context.get("provider")
    )
    ident = str(info["id"])
    live = bool(
        info.get("is_live") or info.get("live_status") == "is_live"
    ) and not str(info.get("extractor_key", "")).lower().startswith("twitchvod")
    date = info.get("upload_date") or info.get("release_date") or ""
    stamp = info.get("timestamp") or info.get("release_timestamp")
    if not date and stamp:
        date = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y%m%d")
    if live:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
    if len(date) != 8 or not date.isdigit():
        date = (
            ""  # Undated collections use season zero; standalone movies need no year.
        )
    creator = str(
        info.get("channel_id")
        or info.get("uploader_id")
        or context.get("creator_id")
        or ident
    )
    kind = "movie" if live or not context.get("collection") else "episode"
    # Live identity follows the creator, so polls update one temporary movie rather
    # than adding a new arbitrary movie on every refresh.
    key = f"{source}:live:{creator}" if live else f"{source}:video:{ident}"
    return {
        "key": key,
        "provider": source,
        "source_id": ident,
        "url": info.get("webpage_url") or info.get("original_url"),
        "title": info.get("title") or ident,
        "description": info.get("description") or "",
        "creator_id": creator,
        "creator_name": context.get("alias")
        or info.get("channel")
        or info.get("uploader")
        or context.get("name")
        or source,
        "creator_description": context.get("description") or "",
        "creator_art": context.get("art"),
        "creator_year": context.get("year"),
        "categories": info.get("categories") or [],
        "published": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if date else "",
        "timestamp": stamp or 0,
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail"),
        "kind": kind,
        "is_live": live,
    }
