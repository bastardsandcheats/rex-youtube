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

from .provider_registry import definitions, get_definition, identify

LOG = logging.getLogger("providers")

SOURCES = {key: module.NAME for key, module in definitions().items()}


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
    return identify(extractor)


@functools.lru_cache(maxsize=1)
def extractors():
    """Cache yt-dlp’s offline URL classifiers; Generic is the final fallback."""
    return [cls for cls in gen_extractor_classes() if cls.ie_key() != "Generic"]


def detect(url):
    """Offline URL classification; extraction's actual identity remains authoritative."""
    for cls in extractors():
        if cls.suitable(url):
            return provider(cls.ie_key())
    return "generic"


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
    definition = get_definition(detect(url))
    if definition and hasattr(definition, "resolve"):
        resolved = definition.resolve(url, flat=flat)
        if resolved is not None:
            return resolved
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
    if limit:
        opts["playlistend"] = limit
    if definition and hasattr(definition, "prepare"):
        url, extra = definition.prepare(url)
        opts.update(extra)
    if isinstance(opts.get("impersonate"), str):
        opts["impersonate"] = ImpersonateTarget.from_str(opts["impersonate"])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                actual = get_definition(
                    provider(info.get("extractor_key") or info.get("extractor"))
                )
                if actual and hasattr(actual, "enrich"):
                    actual.enrich(info, flat=flat)
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
        # Prefer yt-dlp for ordinary manifests and files too. A generic Streamlink
        # plugin may claim a finite DASH URL without exposing playable stream URLs.
        # Only an unsupported web page falls back to a live Streamlink plugin.
        if detect(url) == "generic" and re.search(r"Unsupported URL", str(error), re.I):
            import streamlink

            try:
                streamlink.Streamlink().resolve_url(url)
            except streamlink.NoPluginError:
                pass
            else:
                return streamlink_live(url)
        # A network/API failure must never be mistaken for a live -> offline transition.
        if re.search(
            r"currently offline|is offline|not currently live|not broadcasting|livestream has ended|not live",
            str(error),
            re.I,
        ):
            raise Unavailable("Source is currently offline") from error
        raise


def entry_url(entry):
    """Return a flat entry’s page URL or delegate ID expansion to its provider."""
    url = entry.get("webpage_url") or entry.get("url") or ""
    if urlsplit(url).scheme in ("http", "https"):
        return url
    definition = get_definition(provider(entry.get("ie_key")))
    if definition and hasattr(definition, "entry_url"):
        return definition.entry_url(entry)
    raise ValueError("Extractor did not provide a playable webpage URL")


@functools.lru_cache(maxsize=128)
def creator_metadata(source, ident):
    """Fill creator biographies/avatars from public APIs that need no account."""
    definition = get_definition(source)
    try:
        if definition and hasattr(definition, "creator"):
            return definition.creator(ident)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        LOG.debug("Public creator metadata unavailable for %s", source)
    return {}


def source_targets(url, check_live=None):
    """Expand a configured channel without inventing live endpoints for unknown sites."""
    definition = get_definition(detect(url))
    targets = (
        definition.targets(url)
        if definition and hasattr(definition, "targets")
        else [url]
    )
    if definition and hasattr(definition, "live_url"):
        live_url = definition.live_url(url)
        if check_live is False:
            targets = [
                target for target in targets if target != definition.live_url(target)
            ]
        elif check_live is True and live_url and live_url not in targets:
            targets.append(live_url)
    return targets


def configured_targets(entry):
    """Share channel/live expansion between the poller and configured-source checks."""
    settings = entry["options"]
    for url in source_targets(entry["url"], settings.get("check_live")):
        target_settings = dict(settings)
        definition = get_definition(detect(url))
        if (
            definition
            and hasattr(definition, "live_url")
            and definition.live_url(url) == url
        ):
            target_settings.update(type="live", check_live=True)
        yield {"url": url, "options": target_settings}


def apply_settings(item, settings, context=None):
    """Apply display/placement overrides while retaining the source identity and originals."""
    item = dict(item)
    item.setdefault("source_title", item["title"])
    item.setdefault("source_creator_name", item["creator_name"])
    item.setdefault("source_kind", item["kind"])
    item.setdefault(
        "source_collection",
        (
            bool(context.get("collection"))
            if context is not None
            else item["source_kind"] == "episode"
        ),
    )
    kind = settings.get("type", "auto")
    item["kind"] = (
        "movie"
        if item["is_live"] or kind == "movie"
        else "episode" if kind == "show" else item["source_kind"]
    )
    item["title"] = item["source_title"]
    item["creator_name"] = item["source_creator_name"]
    name = settings.get("name")
    if name and item["kind"] == "episode":
        item["creator_name"] = name
    elif name and (not item["source_collection"] or item["is_live"]):
        item["title"] = name
    item["season"] = (
        int(item["published"][:4] or 0)
        if settings.get("year_seasons", True)
        else settings.get("season", 1)
    )
    item["configured_site"] = settings.get("site", item["provider"])
    item["max_height"] = settings.get("max_height", 1080)
    field = (
        "live_library"
        if item["is_live"]
        else "library" if item["kind"] == "episode" else "movie_library"
    )
    item["library_name"] = settings.get(field)
    return item


def live_status(info):
    """Let a provider distinguish saved recordings from genuine live media."""
    definition = get_definition(
        provider(info.get("extractor_key") or info.get("extractor"))
    )
    if definition and hasattr(definition, "live_status"):
        return definition.live_status(info)
    return bool(info.get("is_live") or info.get("live_status") == "is_live")


def enumerate_source(url, limit=1000):
    """Yield webpage references, leaving expensive per-video extraction to the poller."""
    detected = detect(url)
    definition = get_definition(detected)
    if definition and hasattr(definition, "collection_url"):
        url = definition.collection_url(url)
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
        "name": info.get("channel") or info.get("uploader") or info.get("title"),
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
    """Prefer square creator artwork over a channel banner."""
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
    live = live_status(info)
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
        "creator_name": info.get("channel")
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
