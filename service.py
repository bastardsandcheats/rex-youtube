#!/usr/bin/env python3
"""YouTube -> Rex metadata and HLS relay. Never downloads media to disk."""

import collections
import functools
import hashlib
import io
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import requests
import yt_dlp
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 16_000_000

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "config.json"
CFG = json.loads(
    (
        CONFIG_PATH if CONFIG_PATH.exists() else ROOT / "config" / "config.example.json"
    ).read_text()
)
LOG = logging.getLogger("rex-youtube")
STOP = threading.Event()
# LOCK protects URL/format maps; RESOLVE_LOCK prevents concurrent extraction
# of the same expiring signed URLs. Neither map is persisted in SQLite.
LOCK = threading.RLock()
RESOLVE_LOCK = threading.Lock()
URLS = collections.OrderedDict()
FORMATS = {}


class YTLog:
    def debug(self, msg):
        pass

    def warning(self, msg):
        LOG.debug("yt-dlp: %s", msg)

    def error(self, msg):
        LOG.warning("yt-dlp: %s", msg)


@contextmanager
def db():
    """Open one SQLite connection per operation; never share connections across threads."""
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ROOT / "data" / "metadata.db", timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    """Create metadata tables and apply the small, backwards-compatible schema migration."""
    with db() as con:
        con.executescript("""PRAGMA journal_mode=WAL;
 CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,name TEXT,description TEXT);
 CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY,channel_id TEXT,title TEXT,description TEXT,published TEXT,duration REAL,episode INTEGER,info TEXT,rating_key TEXT,status TEXT,error TEXT,checked REAL);
 CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
 """)
    with db() as con:
        if "art" not in {r["name"] for r in con.execute("PRAGMA table_info(channels)")}:
            con.execute("ALTER TABLE channels ADD COLUMN art TEXT")


def state(key, value=None):
    with db() as con:
        if value is not None:
            con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))
            return
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def yt(url, flat=False):
    """Extract metadata or a channel index without downloading media or enabling disk cache."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cachedir": False,
        "logger": YTLog(),
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "js_runtimes": {"node": {}},
        "extract_flat": "in_playlist" if flat else False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def choose(info):
    """Select compatible HLS video and prefer the original-language audio rendition."""
    fs = info.get("formats", [])
    video = [
        f
        for f in fs
        if f.get("protocol") in ("m3u8_native", "m3u8")
        and str(f.get("vcodec", "")).startswith("avc")
        and (f.get("height") or 99999) <= CFG["max_height"]
    ]
    audio = [
        f
        for f in fs
        if f.get("protocol") in ("m3u8_native", "m3u8") and f.get("vcodec") == "none"
    ]
    if not video or not audio:
        raise RuntimeError("No compatible H.264/AAC HLS streams are available")
    v = max(video, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    a = max(
        audio,
        key=lambda f: (
            "original" in str(f.get("format_note", "")),
            str(f.get("language", "")).startswith("en"),
            f.get("tbr") or 0,
            f.get("format_id", ""),
        ),
    )
    return v, a


def prepare_hls(row):
    """Refresh a stored video’s format metadata before publishing its current delivery version."""
    info = yt("https://www.youtube.com/watch?v=" + row["id"])
    v, a = choose(info)
    # Keep this delivery version stable: Rex retains analysis for an existing file path.
    saved = {
        "delivery": "hls-v2",
        "thumbnail": info.get("thumbnail"),
        "media_info": media_info(info, v, a),
    }
    with LOCK:
        FORMATS[row["id"]] = (time.time(), v, a, info.get("http_headers") or {})
    with db() as con:
        con.execute(
            "UPDATE videos SET info=?,status='ready' WHERE id=?",
            (json.dumps(saved), row["id"]),
        )
    row["info"] = json.dumps(saved)
    return row


def media_info(info, v, a):
    """Describe the controlled HLS master; its audio rendition becomes FFmpeg stream zero."""
    # FFmpeg enumerates this master playlist's separate audio rendition first.
    return {
        "format": {
            "format_name": "hls",
            "duration": str(info["duration"]),
            "nb_streams": 2,
        },
        "streams": [
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "h264",
                "width": v.get("width"),
                "height": v.get("height"),
                "avg_frame_rate": str(v.get("fps") or 30),
                "r_frame_rate": str(v.get("fps") or 30),
                "pix_fmt": "yuv420p",
            },
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": a.get("audio_channels") or 2,
                "sample_rate": str(a.get("asr") or 44100),
                "tags": {"language": a.get("language") or "eng"},
            },
        ],
    }


@functools.lru_cache(maxsize=1)
def local_url():
    """Advertise the relay address reachable by Rex, or use an explicit source_url override."""
    if CFG.get("source_url"):
        return CFG["source_url"].rstrip("/")
    target = urlsplit(CFG["rex_url"])
    # UDP connect selects the outbound interface without sending a packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as con:
        con.connect((target.hostname, target.port or 80))
        host = con.getsockname()[0]
    return f'http://{host}:{CFG["port"]}'


def rex_headers(extra=None):
    """Attach credentials only to Rex requests, never to YouTube or image downloads."""
    headers = {"Accept": "application/json"}
    token = os.environ.get("REX_TOKEN") or CFG.get("rex_token")
    if token:
        headers["X-Plex-Token"] = token
    if extra:
        headers.update(extra)
    return headers


def rex(method, path, body=None, params=None):
    r = requests.request(
        method,
        CFG["rex_url"].rstrip("/") + path,
        json=body,
        params=params,
        headers=rex_headers(),
        timeout=(5, 180),
    )
    r.raise_for_status()
    return r.json() if r.content and "json" in r.headers.get("Content-Type", "") else {}


def setup_rex():
    """Create or reuse the TV library, then register this HTTP relay as its feed source."""
    sections = (
        rex("GET", "/library/sections").get("MediaContainer", {}).get("Directory", [])
    )
    lib = next(
        (
            x
            for x in sections
            if x.get("title") == CFG["library"] and x.get("type") == "show"
        ),
        None,
    )
    if lib is None:
        rex(
            "POST",
            "/library/sections",
            params={
                "name": CFG["library"],
                "type": "show",
                "agent": "tv.plex.agents.series",
                "scanner": "Plex TV Series",
                "language": "en-US",
                "location": "/rex-youtube/youtube",
            },
        )
        lib = next(
            x
            for x in rex("GET", "/library/sections")["MediaContainer"]["Directory"]
            if x.get("title") == CFG["library"] and x.get("type") == "show"
        )
    state("library", lib["key"])
    rex(
        "PUT",
        "/rex/sources/" + CFG["source"],
        {"url": local_url() + "/stream", "mount": "/rex-youtube"},
    )
    LOG.info("Using Rex library %s (section %s)", CFG["library"], lib["key"])


def channel_art(listing):
    thumbs = listing.get("thumbnails") or []
    squares = [
        t
        for t in thumbs
        if t.get("width") and t.get("height") and abs(t["width"] - t["height"]) < 10
    ]
    if squares:
        return max(squares, key=lambda t: t["width"])["url"]
    return next((t["url"] for t in thumbs if "avatar" in str(t.get("id"))), None)


def jpeg_art(data):
    """Convert downloaded artwork in memory to the JPEG representation Rex stores."""
    # Rex's upload endpoint stores artwork as JPEG: normalize the bytes to match.
    if len(data) > 8_000_000:
        raise ValueError("Thumbnail too large")
    with Image.open(io.BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=92)
    return output.getvalue()


def upload_art_bytes(key, data):
    upload = requests.post(
        CFG["rex_url"].rstrip("/") + "/library/metadata/" + str(key) + "/posters",
        data=jpeg_art(data),
        headers=rex_headers({"Content-Type": "image/jpeg"}),
        timeout=20,
    )
    upload.raise_for_status()


def upload_art(key, url):
    image = requests.get(url, timeout=(5, 15))
    image.raise_for_status()
    if len(image.content) > 8_000_000:
        raise ValueError("Thumbnail too large")
    upload_art_bytes(key, image.content)


def edit(key, kind, fields):
    params = {"type": kind}
    for name, value in fields.items():
        params[name + ".value"] = value
        params[name + ".locked"] = 1
    rex("PUT", "/library/metadata/" + str(key), params=params)


def reorder(channel_id):
    """Number each upload year chronologically and queue changed placements for delivery."""
    # Recompute from upload dates, never from YouTube's newest-first listing order.
    with db() as con:
        rows = con.execute(
            "SELECT id,published,episode FROM videos WHERE channel_id=? AND info IS NOT NULL ORDER BY published,id",
            (channel_id,),
        ).fetchall()
        # Use the channel listing to break ties between uploads on the same date.
        order = {
            ident: index
            for index, ident in enumerate(
                json.loads(state("order:" + channel_id) or "[]")
            )
        }
        rows = sorted(
            rows,
            key=lambda row: (
                row["published"] or "",
                order.get(row["id"], len(order)),
                row["id"],
            ),
        )
        counts = {}
        for row in rows:
            year = (row["published"] or "")[:4]
            if not year.isdigit():
                continue
            counts[year] = counts.get(year, 0) + 1
            if row["episode"] != counts[year]:
                con.execute(
                    "UPDATE videos SET episode=?,status='ready' WHERE id=?",
                    (counts[year], row["id"]),
                )


def flush_ready():
    """Retry metadata awaiting delivery without re-indexing videos already published."""
    with db() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM videos WHERE status='ready' ORDER BY published,id"
            )
        ]
    for row in rows:
        try:
            if json.loads(row["info"]).get("delivery") != "hls-v2":
                row = prepare_hls(row)
            publish(row)
        except Exception:
            LOG.exception("Pending metadata delivery failed for %s", row["id"])


def publish(row):
    """Publish one placed episode, preserve upgrade playback state, and apply YouTube metadata."""
    info = json.loads(row["info"])
    channel = None
    with db() as con:
        channel = dict(
            con.execute(
                "SELECT * FROM channels WHERE id=?", (row["channel_id"],)
            ).fetchone()
        )
    item = {
        "id": "yt-hls2-" + row["id"],
        "path": f'youtube/{row["channel_id"]}/{row["id"]}.v2.m3u8',
        "library": state("library"),
        "name": channel["name"],
        "release": "youtube:" + row["channel_id"],
        "kind": "episode",
        "season": int(row["published"][:4]),
        "episode": row["episode"],
        "media_info": info["media_info"],
    }
    response = rex("POST", "/rex/feed/" + CFG["source"] + "/items", {"items": [item]})
    if response.get("rejected") or response.get("held"):
        raise RuntimeError("Rex feed refusal: " + str(response))
    receipts = rex("GET", "/rex/feed/" + CFG["source"] + "/items").get("items", [])
    receipt = next(
        x for x in receipts if x["id"] == item["id"] and not x.get("deleted")
    )
    key = str(receipt["ratingKey"])
    # Earlier delivery layouts used different source IDs. Retire only this video's
    # old versions, copying watched/resume state before removing their feed entries.
    for old in [
        x
        for x in receipts
        if x["id"]
        in ("yt-" + row["id"], "yt-progressive-" + row["id"], "yt-hls-" + row["id"])
        and not x.get("deleted")
    ]:
        previous = rex("GET", "/library/metadata/" + str(old["ratingKey"]))[
            "MediaContainer"
        ]["Metadata"][0]
        if str(old["ratingKey"]) != key:
            if previous.get("viewCount"):
                rex(
                    "GET",
                    "/:/scrobble",
                    params={"key": key, "identifier": "com.plexapp.plugins.library"},
                )
            if previous.get("viewOffset"):
                rex(
                    "GET",
                    "/:/progress",
                    params={
                        "key": key,
                        "time": previous["viewOffset"],
                        "identifier": "com.plexapp.plugins.library",
                        "state": "stopped",
                    },
                )
        rex("DELETE", "/rex/feed/" + CFG["source"] + "/items", {"ids": [old["id"]]})
        retired = json.loads(state("retired_keys") or "[]")
        if str(old["ratingKey"]) != key and str(old["ratingKey"]) not in retired:
            retired.append(str(old["ratingKey"]))
            state("retired_keys", json.dumps(retired))
    m = rex("GET", "/library/metadata/" + key)["MediaContainer"]["Metadata"][0]
    # These are custom videos: keep YouTube metadata instead of matching Plex titles.
    fields = {
        "title": row["title"],
        "summary": row["description"],
        "index": row["episode"],
    }
    if row["published"]:
        fields["originallyAvailableAt"] = row["published"]
    edit(key, 4, fields)
    parent = m.get("grandparentRatingKey")
    if parent:
        edit(parent, 2, {"title": channel["name"], "summary": channel["description"]})
    if info.get("thumbnail"):
        try:
            upload_art(key, info["thumbnail"])
        except (requests.RequestException, ValueError):
            LOG.warning("Could not set thumbnail for %s", row["id"])
    if channel.get("art"):
        for owner in (parent, m.get("parentRatingKey")):
            if owner:
                try:
                    upload_art(owner, channel["art"])
                except (requests.RequestException, ValueError):
                    LOG.warning("Could not set channel badge for %s", owner)
    with db() as con:
        con.execute(
            "UPDATE videos SET status=?,rating_key=?,error=NULL WHERE id=?",
            ("published", key, row["id"]),
        )
    LOG.info("Published %s: %s (Rex %s)", channel["name"], row["title"], key)


def sync():
    """Index a bounded batch, commit local metadata, then publish pending episodes to Rex."""
    setup_rex()
    flush_ready()
    for line in (ROOT / "config" / "channels.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, alias = line.partition("|")
        url = url.strip()
        alias = alias.strip()
        if "/@" in url and not re.search(r"/(videos|streams|shorts)/?$", url):
            url = url.rstrip("/") + "/videos"
        try:
            listing = yt(url, True)
            channel_id = listing.get("channel_id") or listing["id"]
            name = (
                alias
                or listing.get("channel")
                or listing["title"].removesuffix(" - Videos")
            )
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO channels(id,name,description,art) VALUES(?,?,?,?)",
                    (
                        channel_id,
                        name,
                        listing.get("description") or "",
                        channel_art(listing),
                    ),
                )
            entries = list(listing.get("entries") or [listing])
            entries.reverse()
            state("order:" + channel_id, json.dumps([e.get("id") for e in entries]))
            count = 0
            for entry in entries:
                if STOP.is_set():
                    return
                ident = entry.get("id")
                if not ident or not re.fullmatch(r"[\w-]{11}", ident):
                    continue
                with db() as con:
                    old = con.execute(
                        "SELECT * FROM videos WHERE id=?", (ident,)
                    ).fetchone()
                if old and old["status"] == "published":
                    continue
                # Unavailable/member-only uploads should not consume every polling cycle.
                if (
                    old
                    and old["status"] == "error"
                    and time.time() - (old["checked"] or 0) < 86400
                ):
                    continue
                if count >= CFG["batch_size"]:
                    break
                count += 1
                try:
                    info = yt("https://www.youtube.com/watch?v=" + ident)
                    if info.get("is_live") or not info.get("duration"):
                        continue
                    v, a = choose(info)
                    saved = {
                        "delivery": "hls-v2",
                        "thumbnail": info.get("thumbnail"),
                        "media_info": media_info(info, v, a),
                    }
                    with LOCK:
                        FORMATS[ident] = (
                            time.time(),
                            v,
                            a,
                            info.get("http_headers") or {},
                        )
                    date = info.get("upload_date") or ""
                    date = (
                        f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else ""
                    )
                    # Commit a retryable 'ready' row before making any feed request to Rex.
                    with db() as con:
                        episode = (
                            old["episode"]
                            if old and old["episode"]
                            else con.execute(
                                "SELECT COALESCE(MAX(episode),0)+1 FROM videos WHERE channel_id=?",
                                (channel_id,),
                            ).fetchone()[0]
                        )
                        con.execute(
                            "INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                ident,
                                channel_id,
                                info["title"],
                                info.get("description") or "",
                                date,
                                info["duration"],
                                episode,
                                json.dumps(saved),
                                old["rating_key"] if old else None,
                                "ready",
                                None,
                                time.time(),
                            ),
                        )
                except Exception as error:
                    LOG.warning("Could not add video %s: %s", ident, str(error)[:400])
                    with db() as con:
                        con.execute(
                            "INSERT INTO videos(id,channel_id,status,error,checked) VALUES(?,?,'error',?,?) ON CONFLICT(id) DO UPDATE SET error=excluded.error,checked=excluded.checked,status=CASE WHEN videos.info IS NULL THEN 'error' ELSE videos.status END",
                            (ident, channel_id, type(error).__name__, time.time()),
                        )
            reorder(channel_id)
            flush_ready()
            LOG.info(
                "Channel check complete: %s; %s new entries processed", name, count
            )
        except Exception:
            LOG.exception("Channel check failed")


def resolve(ident):
    """Refresh expiring signed stream URLs on demand; retain them only in memory."""
    with RESOLVE_LOCK:
        with LOCK:
            cached = FORMATS.get(ident)
        if cached and time.time() - cached[0] < 600:
            return cached[1:]
        info = yt("https://www.youtube.com/watch?v=" + ident)
        v, a = choose(info)
        headers = info.get("http_headers") or {}
        with LOCK:
            FORMATS[ident] = (time.time(), v, a, headers)
        return v, a, headers


def token_url(ident, url, headers, ext="ts"):
    """Map an approved upstream URL to a bounded, opaque, in-memory relay token."""
    host = urlsplit(url).hostname or ""
    if not any(
        host == domain or host.endswith("." + domain)
        for domain in ("googlevideo.com", "youtube.com")
    ):
        raise ValueError("Unexpected upstream host")
    token = hashlib.sha256(url.encode()).hexdigest()[:32]
    with LOCK:
        URLS[(ident, token)] = (url, headers)
        URLS.move_to_end((ident, token))
        while len(URLS) > 20000:
            URLS.popitem(last=False)
    return f"{local_url()}/hls/{ident}/{token}.{ext}"


def rewrite(ident, text, base, headers):
    """Rewrite playlist segments, encryption keys and initialization maps to relay URLs."""

    def uri(match):
        return (
            'URI="'
            + token_url(
                ident,
                urljoin(base, match[1]),
                headers,
                "mp4" if "#EXT-X-MAP" in line else "key",
            )
            + '"'
        )

    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = token_url(ident, urljoin(base, line), headers)
        elif 'URI="' in line:
            line = re.sub(r'URI="([^"]+)"', uri, line)
        lines.append(line)
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.route(True)

    def do_GET(self):
        self.route(False)

    def do_DELETE(self):
        self.send_error(403, "Deletion is not supported")

    def data(self, body, typ, head=False):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def route(self, head):
        response_started = False
        try:
            path = urlsplit(self.path).path
            if path == "/health":
                return self.data('{"ok":true}', "application/json", head)
            match = re.fullmatch(
                r"/(?:master|stream)/youtube/([\w-]+)/([\w-]{11})(?:\.v2)?\.m3u8", path
            )
            if match:
                channel, ident = match.groups()
                with db() as con:
                    row = con.execute(
                        "SELECT id FROM videos WHERE id=? AND channel_id=? AND info IS NOT NULL",
                        (ident, channel),
                    ).fetchone()
                if not row:
                    return self.send_error(404)
                v, a, headers = resolve(ident)
                vu = token_url(ident, v["url"], headers, "m3u8")
                au = token_url(ident, a["url"], headers, "m3u8")
                master = f'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",DEFAULT=YES,AUTOSELECT=YES,URI="{au}"\n#EXT-X-STREAM-INF:BANDWIDTH={int(((v.get("tbr") or 2500)+(a.get("tbr") or 128))*1000)},RESOLUTION={v["width"]}x{v["height"]},CODECS="{v["vcodec"]},mp4a.40.2",AUDIO="audio"\n{vu}\n'
                return self.data(master, "application/vnd.apple.mpegurl", head)
            match = re.fullmatch(
                r"/hls/([\w-]{11})/([0-9a-f]{32})\.(?:m3u8|ts|mp4|key)", path
            )
            if not match:
                return self.send_error(404)
            with LOCK:
                upstream = URLS.get(match.groups())
            # Evicted tokens cannot be reconstructed from the request. The player must
            # reload its master playlist, which refreshes the upstream URLs.
            if not upstream:
                return self.send_error(410, "Reload the playlist to refresh playback")
            url, headers = upstream
            headers = dict(headers)
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            with requests.get(
                url, headers=headers, stream=True, timeout=(8, 40)
            ) as response:
                if response.status_code not in (200, 206):
                    return self.send_error(
                        502, "YouTube stream unavailable; reload to refresh"
                    )
                typ = response.headers.get("Content-Type", "application/octet-stream")
                # YouTube segment URLs can contain '.m3u8'; only MIME type identifies
                # manifests. Decoding a binary segment as text would corrupt playback.
                if "mpegurl" in typ.lower():
                    return self.data(
                        rewrite(match[1], response.text, url, headers),
                        "application/vnd.apple.mpegurl",
                        head,
                    )
                self.send_response(response.status_code)
                for key in (
                    "Content-Type",
                    "Content-Length",
                    "Content-Range",
                    "Accept-Ranges",
                ):
                    if key in response.headers:
                        self.send_header(key, response.headers[key])
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                if "Content-Length" not in response.headers:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                response_started = True
                if not head:
                    # Forward bounded chunks rather than buffering a video or writing it to disk.
                    for chunk in response.iter_content(65536):
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            LOG.warning(
                "Stream request failed: %s (errno=%s)",
                type(error).__name__,
                getattr(error, "errno", None),
            )
            # A second HTTP response would corrupt the media already being sent.
            # Closing lets the player detect a short read and retry the segment.
            if response_started:
                self.close_connection = True
                return
            try:
                self.send_error(502, "Stream resolution failed; retry shortly")
            except OSError:
                pass


def worker():
    while not STOP.is_set():
        try:
            sync()
        except Exception:
            LOG.exception("Sync failed; will retry")
        STOP.wait(CFG["interval_seconds"])


def main():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "Create config/config.json from config/config.example.json and set your Rex address."
        )
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, str(CFG.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                ROOT / "logs" / "rex-youtube.log", maxBytes=5_000_000, backupCount=3
            ),
        ],
    )
    init_db()
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    server.daemon_threads = True
    threading.Thread(target=worker, daemon=True, name="youtube-sync").start()
    LOG.info(
        "YouTube relay listening on port %s; metadata only, no media cache", CFG["port"]
    )
    try:
        server.serve_forever()
    finally:
        STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
