#!/usr/bin/env python3
"""A small anonymous web-video feed and HTTP relay for Rex."""

import collections
import functools
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageOps

import playback
import providers

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
LOCK = threading.RLock()
RESOLVE_LOCK = threading.Lock()
URLS = collections.OrderedDict()
FORMATS = {}
LIBRARIES = {}


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


def state(key, value=None):
    with db() as con:
        if value is not None:
            con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, str(value)))
            return
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


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


def init_db():
    """Add the generic item table; migrate old videos without changing Rex identities."""
    with db() as con:
        con.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS items(key TEXT PRIMARY KEY,metadata TEXT NOT NULL,
            feed_id TEXT NOT NULL,relay_path TEXT NOT NULL,rating_key TEXT,
            status TEXT NOT NULL DEFAULT 'ready');
        CREATE TABLE IF NOT EXISTS failures(url TEXT PRIMARY KEY,checked REAL,error TEXT);
        """)
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if (
            "videos" not in tables
            or con.execute(
                "SELECT 1 FROM state WHERE key='generic-migration'"
            ).fetchone()
        ):
            return
        channels = {r["id"]: dict(r) for r in con.execute("SELECT * FROM channels")}
        for row in con.execute("SELECT * FROM videos WHERE info IS NOT NULL"):
            channel = channels.get(row["channel_id"], {})
            item = providers.normalize(
                {
                    "id": row["id"],
                    "extractor_key": "Youtube",
                    "webpage_url": "https://www.youtube.com/watch?v=" + row["id"],
                    "title": row["title"],
                    "description": row["description"],
                    "channel_id": row["channel_id"],
                    "upload_date": row["published"].replace("-", ""),
                    "duration": row["duration"],
                    "thumbnail": json.loads(row["info"]).get("thumbnail"),
                },
                {
                    "collection": True,
                    "name": channel.get("name"),
                    "description": channel.get("description"),
                    "art": channel.get("art"),
                },
            )
            item.update(
                episode=row["episode"],
                media_info=json.loads(row["info"])["media_info"],
                delivery="hls",
            )
            con.execute(
                "INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)",
                (
                    item["key"],
                    json.dumps(item),
                    "yt-hls2-" + row["id"],
                    f'youtube/{row["channel_id"]}/{row["id"]}.v2.m3u8',
                    row["rating_key"],
                    "ready",
                ),
            )
        con.execute("INSERT OR REPLACE INTO state VALUES('generic-migration','1')")


def library_name(item):
    title = providers.SOURCES.get(item["provider"], item["provider"].title())
    if item["is_live"]:
        return title + " Live"
    if item["kind"] == "episode":
        return CFG.get("library", title) if item["provider"] == "youtube" else title
    return title + " Videos"


def ensure_library(item):
    """Create simple provider/kind libraries, independently of the configured URLs."""
    name, kind = library_name(item), "show" if item["kind"] == "episode" else "movie"
    key = (name, kind)
    if key not in LIBRARIES:
        sections = rex("GET", "/library/sections")["MediaContainer"].get(
            "Directory", []
        )
        section = next(
            (r for r in sections if r.get("title") == name and r.get("type") == kind),
            None,
        )
        if not section:
            directory = item["provider"] + (
                "-live" if item["is_live"] else "-movies" if kind == "movie" else ""
            )
            rex(
                "POST",
                "/library/sections",
                params={
                    "name": name,
                    "type": kind,
                    "agent": (
                        "tv.plex.agents.series"
                        if kind == "show"
                        else "tv.plex.agents.movie"
                    ),
                    "scanner": "Plex TV Series" if kind == "show" else "Plex Movie",
                    "language": "en-US",
                    "location": "/rex-youtube/" + directory,
                },
            )
            sections = rex("GET", "/library/sections")["MediaContainer"].get(
                "Directory", []
            )
            section = next(
                r for r in sections if r.get("title") == name and r.get("type") == kind
            )
        LIBRARIES[key] = str(section["key"])
    return LIBRARIES[key]


def setup_rex():
    LIBRARIES.clear()
    rex(
        "PUT",
        "/rex/sources/" + CFG["source"],
        {"url": local_url() + "/stream", "mount": "/rex-youtube"},
    )


def save_item(item, plan, source_entry):
    """Persist canonical metadata, never the signed formats and anonymous cookies."""
    item = {
        **item,
        "media_info": playback.media_info(plan),
        "delivery": plan["kind"],
        "source_entry": source_entry,
    }
    suffix = playback.extension(plan)
    digest = hashlib.sha256(item["key"].encode()).hexdigest()[:24]
    directory = item["provider"] + (
        "-live" if item["is_live"] else "-movies" if item["kind"] == "movie" else ""
    )
    with db() as con:
        previous = con.execute(
            "SELECT * FROM items WHERE key=?", (item["key"],)
        ).fetchone()
        if previous:
            old = json.loads(previous["metadata"])
            item["episode"] = old.get("episode", 1)
            con.execute(
                "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                (json.dumps(item), item["key"]),
            )
        else:
            item["episode"] = 1
            con.execute(
                "INSERT INTO items VALUES(?,?,?,?,?,?)",
                (
                    item["key"],
                    json.dumps(item),
                    item["key"],
                    f"{directory}/{digest}.{suffix}",
                    None,
                    "ready",
                ),
            )
    with LOCK:
        FORMATS[item["key"]] = (time.time(), plan)


def reorder(creator_id):
    """Backfilled videos may renumber a year, but feed IDs and watch state stay stable."""
    with db() as con:
        rows = [
            (r, json.loads(r["metadata"])) for r in con.execute("SELECT * FROM items")
        ]
        counts = collections.Counter()
        for row, item in sorted(
            rows,
            key=lambda r: (r[1]["published"], r[1].get("timestamp", 0), r[1]["key"]),
        ):
            if item["kind"] != "episode" or item["creator_id"] != creator_id:
                continue
            group = (item["provider"], item["published"][:4])
            counts[group] += 1
            if item.get("episode") != counts[group]:
                item["episode"] = counts[group]
                con.execute(
                    "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                    (json.dumps(item), row["key"]),
                )


def publish(row):
    """Feed explicit placements, then apply the source's custom title and artwork."""
    item = json.loads(row["metadata"])
    # Upstream accepts video file labels, not manifest extensions. `path` remains
    # the actual manifest URL; `file` is a logical Part label, never a disk file.
    file = (
        "/rex-youtube/"
        + row["relay_path"].rsplit(".", 1)[0]
        + (".ts" if item["is_live"] else ".mp4")
    )
    payload = {
        "id": row["feed_id"],
        "path": row["relay_path"],
        "file": file,
        "library": ensure_library(item),
        "name": item["creator_name"] if item["kind"] == "episode" else item["title"],
        "release": (
            item["provider"] + ":" + item["creator_id"]
            if item["kind"] == "episode"
            else item["key"]
        ),
        "kind": item["kind"],
        "media_info": item["media_info"],
    }
    if item["kind"] == "episode":
        payload.update(season=int(item["published"][:4] or 0), episode=item["episode"])
    response = rex(
        "POST", "/rex/feed/" + CFG["source"] + "/items", {"items": [payload]}
    )
    if response.get("rejected") or response.get("held"):
        raise RuntimeError("Rex refused the media metadata: " + str(response))
    receipts = rex("GET", "/rex/feed/" + CFG["source"] + "/items")["items"]
    receipt = next(
        r for r in receipts if r["id"] == row["feed_id"] and not r.get("deleted")
    )
    key = str(receipt["ratingKey"])
    fields = {"title": item["title"], "summary": item["description"]}
    if item["published"]:
        fields["originallyAvailableAt"] = item["published"]
    edit(key, 4 if item["kind"] == "episode" else 1, fields)
    meta = rex("GET", "/library/metadata/" + key)["MediaContainer"]["Metadata"][0]
    if item.get("thumbnail"):
        try:
            upload_art(key, item["thumbnail"])
        except Exception as error:
            LOG.warning(
                "Thumbnail unavailable for %s: %s", row["key"], type(error).__name__
            )
    if item["kind"] == "episode":
        parent = meta.get("grandparentRatingKey")
        if parent:
            edit(
                parent,
                2,
                {
                    "title": item["creator_name"],
                    "summary": item.get("creator_description", ""),
                },
            )
        if item.get("creator_art"):
            for owner in (parent, meta.get("parentRatingKey")):
                if owner:
                    try:
                        upload_art(owner, item["creator_art"])
                    except Exception as error:
                        LOG.warning(
                            "Channel artwork unavailable: %s", type(error).__name__
                        )
    with db() as con:
        con.execute(
            "UPDATE items SET rating_key=?,status='published' WHERE key=?",
            (key, row["key"]),
        )
    LOG.info("Published %s: %s", item["provider"], item["title"])


def flush_ready():
    with db() as con:
        rows = [
            dict(r) for r in con.execute("SELECT * FROM items WHERE status='ready'")
        ]
    for row in rows:
        try:
            publish(row)
        except Exception as error:
            LOG.warning("Delivery failed for %s: %s", row["key"], str(error)[:250])


def remove_offline(source_entry):
    """Only a positive offline result may remove a transient live movie."""
    with db() as con:
        rows = [
            (r, json.loads(r["metadata"])) for r in con.execute("SELECT * FROM items")
        ]
    for row, item in rows:
        if item["is_live"] and item.get("source_entry") == source_entry:
            rex(
                "DELETE",
                "/rex/feed/" + CFG["source"] + "/items",
                {"ids": [row["feed_id"]]},
            )
            with db() as con:
                con.execute("DELETE FROM items WHERE key=?", (row["key"],))
            LOG.info("Removed offline live item: %s", row["key"])


def sync_source(url, alias):
    count = 0
    creators = set()
    for webpage, context, entry in providers.enumerate_source(
        url, alias, CFG.get("collection_limit", 1000)
    ):
        if STOP.is_set():
            return
        source = providers.provider(
            entry.get("ie_key") or entry.get("extractor_key") or context["provider"]
        )
        key = f'{source}:video:{entry.get("id")}'
        with db() as con:
            old = con.execute("SELECT status FROM items WHERE key=?", (key,)).fetchone()
            failure = con.execute(
                "SELECT checked FROM failures WHERE url=?", (webpage,)
            ).fetchone()
        if old and old["status"] == "published":
            continue
        if failure and time.time() - failure[0] < 86400:
            continue
        if count >= CFG["batch_size"]:
            break
        count += 1
        try:
            info = providers.extract(webpage)
            item = providers.normalize(info, context)
            if info.get("live_status") in ("is_upcoming", "post_live"):
                continue
            plan = playback.choose(info, CFG["max_height"])
            playback.inspect_layout(plan)
            save_item(item, plan, url)
            creators.add(item["creator_id"])
        except providers.Unavailable:
            remove_offline(url)
        except Exception as error:
            LOG.warning("Cannot index %s (%s): %s", source, webpage, str(error)[:200])
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO failures VALUES(?,?,?)",
                    (webpage, time.time(), type(error).__name__),
                )
    for creator in creators:
        reorder(creator)


def sync():
    setup_rex()
    flush_ready()
    source_path = ROOT / "config" / "sources.txt"
    if not source_path.exists():
        source_path = ROOT / "config" / "channels.txt"
    for url, alias in providers.source_lines(source_path):
        # Twitch's public channel page is live; its /videos collection is VODs.
        urls = [url]
        if providers.detect(url) == "twitch" and re.fullmatch(
            r"https?://(?:www\.)?twitch.tv/[\w-]+/?", url
        ):
            urls = [url.rstrip("/") + "/videos", url]
        for target in urls:
            try:
                sync_source(target, alias)
            except providers.Unavailable:
                remove_offline(target)
            except Exception as error:
                LOG.warning("Source check failed (%s): %s", target, str(error)[:200])
    flush_ready()


def resolve(key):
    """Refresh anonymous playback URLs close to use; signed URLs stay in RAM."""
    with RESOLVE_LOCK:
        with LOCK:
            cached = FORMATS.get(key)
        if cached and time.time() - cached[0] < 120:
            return cached[1]
        with db() as con:
            row = con.execute(
                "SELECT metadata FROM items WHERE key=?", (key,)
            ).fetchone()
        if not row:
            raise KeyError("Unknown media item")
        item = json.loads(row[0])
        info = providers.extract(item["url"])
        plan = playback.choose(info, CFG["max_height"])
        with LOCK:
            FORMATS[key] = (time.time(), plan)
        return plan


def token_url(ident, url, headers, ext="ts"):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("Upstream must be an HTTP(S) media URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Private upstream addresses are not relayed")
    token = hashlib.sha256(
        (url + json.dumps(headers, sort_keys=True)).encode()
    ).hexdigest()[:32]
    with LOCK:
        URLS[(ident, token)] = (url, headers)
        while len(URLS) > 20000:
            URLS.popitem(last=False)
    return f"{local_url()}/hls/{ident}/{token}.{ext}"


# Small compatibility helpers for the original public example/tests.
def choose(info):
    try:
        plan = playback.choose(info, CFG["max_height"])
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return plan["video"], plan.get("audio")


def media_info(info, video, audio):
    return playback.media_info(
        {
            "kind": "hls",
            "video": video,
            "audio": audio,
            "duration": info["duration"],
            "live": False,
        }
    )


channel_art = providers.channel_art


def rewrite(ident, text, base, headers):
    return playback.rewrite_hls(
        text, base, headers, lambda url, h, ext: token_url(ident, url, h, ext)
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # Players cancel parallel segment requests when stopping or seeking.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.route(False)

    def do_HEAD(self):
        self.route(True)

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

    def relay(self, url, headers, head, ident):
        """Forward bytes and ranges, rewriting HLS playlists using their real MIME type."""
        headers = dict(headers)
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        with requests.get(
            url, headers=headers, stream=True, timeout=(8, 40)
        ) as response:
            if response.status_code not in (200, 206):
                return self.send_error(
                    502, "Upstream playback unavailable; reload to resolve again"
                )
            typ = response.headers.get("Content-Type", "application/octet-stream")
            if "mpegurl" in typ.lower():
                text = playback.rewrite_hls(
                    response.text,
                    response.url or url,
                    headers,
                    lambda u, h, e: token_url(ident, u, h, e),
                )
                return self.data(text, "application/vnd.apple.mpegurl", head)
            self.send_response(response.status_code)
            for name in (
                "Content-Type",
                "Content-Length",
                "Content-Range",
                "Accept-Ranges",
            ):
                if name in response.headers:
                    self.send_header(name, response.headers[name])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            if "Content-Length" not in response.headers:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.response_started = True
            if not head:
                for chunk in response.iter_content(65536):
                    self.wfile.write(chunk)

    def live(self, plan, ident, head):
        """Copy-mux live HLS to indefinite TS: no encoding, seeking or disk buffer.

        Stock Rex proxies a Part using its video-file MIME type. Continuous TS
        therefore works for live Part reads where forwarding a manifest does not.
        """
        url = token_url(
            ident, plan["video"]["url"], playback.headers(plan, plan["video"]), "m3u8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.send_header("Connection", "close")
        self.end_headers()
        self.response_started = True
        self.close_connection = True
        if head:
            return
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-f",
                "mpegts",
                "-muxdelay",
                "0",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while chunk := process.stdout.read(32768):
                self.wfile.write(chunk)
        finally:
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def route(self, head):
        self.response_started = False
        try:
            path = urlsplit(self.path).path
            if path == "/health":
                return self.data('{"ok":true}', "application/json", head)
            if path.startswith(("/stream/", "/master/")):
                relative = path.split("/", 2)[2]
                with db() as con:
                    row = con.execute(
                        "SELECT key FROM items WHERE relay_path=?", (relative,)
                    ).fetchone()
                if not row:
                    return self.send_error(404)
                plan = resolve(row[0])
                ident = hashlib.sha256(row[0].encode()).hexdigest()[:24]

                def relay(u, h, e):
                    return token_url(ident, u, h, e)

                if plan["kind"] == "live":
                    return self.live(plan, ident, head)
                if plan["kind"] == "hls":
                    text, url = playback.hls_master(plan, relay)
                    return (
                        self.data(text, "application/vnd.apple.mpegurl", head)
                        if text
                        else self.relay(
                            plan["video"]["url"],
                            playback.headers(plan, plan["video"]),
                            head,
                            ident,
                        )
                    )
                if plan["kind"] == "dash":
                    return self.data(
                        playback.dash_manifest(plan, relay),
                        "application/dash+xml",
                        head,
                    )
                return self.relay(
                    plan["video"]["url"],
                    playback.headers(plan, plan["video"]),
                    head,
                    ident,
                )
            match = re.fullmatch(
                r"/hls/([\w-]+)/([0-9a-f]{32})\.(?:m3u8|ts|mp4|webm|key)", path
            )
            if not match:
                return self.send_error(404)
            with LOCK:
                upstream = URLS.get(match.groups())
            if not upstream:
                return self.send_error(410, "Reload the playlist to refresh playback")
            return self.relay(*upstream, head, match[1])
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            LOG.warning(
                "Playback failed: %s: %s", type(error).__name__, str(error)[:200]
            )
            if self.response_started:
                self.close_connection = True
            else:
                self.send_error(502, "Playback resolution failed; see service log")


def worker():
    while not STOP.is_set():
        try:
            sync()
        except Exception as error:
            LOG.warning("Refresh failed: %s", str(error)[:250])
        STOP.wait(CFG["interval_seconds"])


def main():
    if not CONFIG_PATH.exists():
        raise SystemExit(
            "Create config/config.json from config/config.example.json first."
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
    threading.Thread(target=worker, daemon=True, name="source-sync").start()
    LOG.info(
        "rex-youtube listening on port %s; metadata only, no media cache", CFG["port"]
    )
    try:
        server.serve_forever()
    finally:
        STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
