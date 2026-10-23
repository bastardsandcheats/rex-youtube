#!/usr/bin/env python3
"""A small anonymous web-video feed and HTTP relay for Rex."""

import collections
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import socket
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

from app import config
from app import playback
from app import live
from app import source as providers

Image.MAX_IMAGE_PIXELS = 16_000_000
ROOT = Path(__file__).resolve().parents[1]
CFG = config.normalize({})
LOG = logging.getLogger("rex-youtube")
STOP = threading.Event()
LOCK = threading.RLock()
RESOLVE_LOCK = threading.Lock()
URLS = collections.OrderedDict()
FORMATS = {}
LIBRARIES = {}
ARTWORK = collections.OrderedDict()
CHECK_LIMIT = threading.BoundedSemaphore(2)


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
    """Read or update small persistent service state."""
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
    """Attach credentials only to Rex requests, never to external media or image downloads."""
    headers = {"Accept": "application/json"}
    token = os.environ.get("REX_TOKEN") or CFG.get("rex_token")
    if token:
        headers["X-Plex-Token"] = token
    if extra:
        headers.update(extra)
    return headers


def rex(method, path, body=None, params=None):
    """Send a JSON request to Rex with credentials restricted to that host."""
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
    """Upload in-memory JPEG artwork to a Rex metadata item."""
    upload = requests.post(
        CFG["rex_url"].rstrip("/") + "/library/metadata/" + str(key) + "/posters",
        data=jpeg_art(data),
        headers=rex_headers({"Content-Type": "image/jpeg"}),
        timeout=20,
    )
    upload.raise_for_status()


def upload_art(key, url):
    # Creator images are reused by every episode; avoid downloading them per row.
    """Fetch optional source artwork and avoid repeating successful uploads."""
    cached = (str(key), url)
    with LOCK:
        if cached in ARTWORK:
            return
    image = requests.get(url, timeout=(5, 15))
    image.raise_for_status()
    if len(image.content) > 8_000_000:
        raise ValueError("Thumbnail too large")
    upload_art_bytes(key, image.content)
    with LOCK:
        ARTWORK[cached] = True
        while len(ARTWORK) > 512:
            ARTWORK.popitem(last=False)


def edit(key, kind, fields):
    """Set and lock source-supplied metadata through Rex’s library API."""
    params = {"type": kind}
    for name, value in fields.items():
        params[name + ".value"] = value
        params[name + ".locked"] = 1
    rex("PUT", "/library/metadata/" + str(key), params=params)


def init_db():
    """Create the current metadata schema; provider media stays on its upstream host."""
    with db() as con:
        con.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS items(key TEXT PRIMARY KEY,metadata TEXT NOT NULL,
            feed_id TEXT NOT NULL,relay_path TEXT NOT NULL,rating_key TEXT,
            status TEXT NOT NULL DEFAULT 'ready');
        CREATE TABLE IF NOT EXISTS failures(url TEXT PRIMARY KEY,checked REAL,error TEXT);
        """)


def library_name(item):
    """Select the configured show/movie/live library or a provider default."""
    if item.get("library_name"):
        return item["library_name"]
    title = providers.SOURCES.get(item["provider"], item["provider"].title())
    if item["is_live"]:
        return "Live Streams"
    if item["kind"] == "episode":
        return title
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
    """Register this feed source and advertise the HTTP relay address."""
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
            group = (
                item["provider"],
                item.get("season", int(item["published"][:4] or 0)),
            )
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
        payload.update(
            season=item.get("season", int(item["published"][:4] or 0)),
            episode=item["episode"],
        )
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
    fields = {
        "title": item["title"],
        "studio": providers.SOURCES.get(item["provider"], item["provider"].title()),
    }
    if item.get("description"):
        fields["summary"] = item["description"]
    if item["published"]:
        fields["originallyAvailableAt"] = item["published"]
        fields["year"] = int(item["published"][:4])
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
            creator_fields = {"title": item["creator_name"], "studio": fields["studio"]}
            if item.get("creator_description"):
                creator_fields["summary"] = item["creator_description"]
            if item.get("creator_year"):
                creator_fields["year"] = int(item["creator_year"])
            edit(parent, 2, creator_fields)
        # Prefer creator artwork; use an episode thumbnail when an extractor has no avatar.
        creator_art = item.get("creator_art") or item.get("thumbnail")
        if creator_art:
            for owner in (parent, meta.get("parentRatingKey")):
                if owner:
                    try:
                        upload_art(owner, creator_art)
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
    """Deliver pending catalog rows without letting one failure stop the rest."""
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


def sync_source(url, settings):
    """Index a bounded batch with source-specific naming, placement and quality."""
    count = 0
    creators = set()
    for webpage, context, entry in providers.enumerate_source(
        url,
        limit=settings["max_items"],
    ):
        if STOP.is_set():
            return
        source = providers.provider(
            entry.get("ie_key") or entry.get("extractor_key") or context["provider"]
        )
        key = f'{source}:video:{entry.get("id")}'
        with db() as con:
            old = con.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
            failure = con.execute(
                "SELECT checked FROM failures WHERE url=?", (webpage,)
            ).fetchone()
        if old and old["status"] == "published":
            previous = json.loads(old["metadata"])
            updated = providers.apply_settings(previous, settings)
            needs_extract = "source_kind" not in previous or previous.get(
                "max_height", CFG["max_height"]
            ) != settings.get("max_height", CFG["max_height"])
            if not needs_extract and updated != previous:
                with db() as con:
                    con.execute(
                        "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                        (json.dumps(updated), old["key"]),
                    )
                creators.add(updated["creator_id"])
            if not needs_extract:
                continue
        if failure and time.time() - failure[0] < 86400 and not entry.get("is_live"):
            continue
        if count >= settings.get("batch_size", CFG["batch_size"]):
            break
        count += 1
        try:
            info = providers.extract(webpage)
            item = providers.apply_settings(
                providers.normalize(info, context), settings, context
            )
            if settings.get("type") == "live" and not item["is_live"]:
                # A live endpoint returning its finished recording is not a new
                # collection episode. Preserve ordinary VOD sources independently.
                remove_offline(url)
                continue
            if (
                item["is_live"]
                and settings.get("check_live") is False
                and settings.get("type") != "live"
            ):
                continue
            if info.get("live_status") in ("is_upcoming", "post_live") or (
                info.get("extractor_key") == "TwitchVod" and info.get("is_live")
            ):
                continue
            plan = playback.choose(info, settings.get("max_height", CFG["max_height"]))
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
    """Poll enabled YAML sources and their explicitly enabled live targets."""
    setup_rex()
    flush_ready()
    for entry in config.source_entries(CFG):
        for target in providers.configured_targets(entry):
            url = target["url"]
            try:
                sync_source(url, target["options"])
            except providers.Unavailable:
                remove_offline(url)
            except Exception as error:
                LOG.warning("Source check failed (%s): %s", url, str(error)[:200])
    flush_ready()


def resolve(key, force=False):
    """Refresh anonymous playback URLs close to use; signed URLs stay in RAM."""
    with RESOLVE_LOCK:
        with LOCK:
            cached = FORMATS.get(key)
        if not force and cached and time.time() - cached[0] < 120:
            return cached[1]
        with db() as con:
            row = con.execute(
                "SELECT metadata FROM items WHERE key=?", (key,)
            ).fetchone()
        if not row:
            raise KeyError("Unknown media item")
        item = json.loads(row[0])
        info = providers.extract(item["url"])
        plan = playback.choose(info, item.get("max_height", CFG["max_height"]))
        with LOCK:
            FORMATS[key] = (time.time(), plan)
        return plan


def token_url(ident, url, headers, ext="ts"):
    """Issue a bounded opaque relay reference; keep upstream credentials in RAM."""
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

    def do_POST(self):
        """Administrative, bounded checks; subprocesses never touch the live catalog."""
        if urlsplit(self.path).path != "/api/providers/check":
            return self.send_error(404)
        configured = os.environ.get("REX_TOKEN") or CFG.get("rex_token")
        local = ipaddress.ip_address(self.client_address[0]).is_loopback
        if not local and not (
            configured
            and hmac.compare_digest(
                self.headers.get("X-Plex-Token", "").encode(), str(configured).encode()
            )
        ):
            return self.send_error(403, "Use loopback or the configured Rex token")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= 8192
                or self.headers.get_content_type() != "application/json"
            ):
                return self.send_error(400, "Send a JSON body of at most 8192 bytes")
            request = json.loads(self.rfile.read(length))
            from app import checks, reporting

            if not isinstance(request, dict):
                raise ValueError("Expected an object containing url")
            checks.validate_url(request.get("url"))
            seconds = request.get("seconds", 4)
            if (
                not isinstance(seconds, (int, float))
                or isinstance(seconds, bool)
                or not 1 <= seconds <= 15
            ):
                raise ValueError("seconds must be between 1 and 15")
        except (ValueError, TypeError):
            return self.send_error(400, "Invalid url, seconds or JSON body")
        if not CHECK_LIMIT.acquire(blocking=False):
            return self.send_error(429, "Two provider checks are already running")
        try:
            result = checks.check_url(request["url"], seconds=seconds)
            reporting.write_reports([result], ROOT / "reports")
            self.data(json.dumps(result), "application/json")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            CHECK_LIMIT.release()

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

    def live(self, plan, key, head):
        if not live.LIMIT.acquire(blocking=False):
            return self.send_error(503, "All live playback slots are in use")
        relay = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Connection", "close")
            self.end_headers()
            self.response_started = True
            self.close_connection = True
            if head:
                return
            relay = live.LiveRelay(
                plan, lambda: resolve(key, force=True), CFG.get("live_buffer_mb", 16)
            )
            for chunk in relay.chunks():
                self.wfile.write(chunk)
        finally:
            if relay:
                relay.close()
            live.LIMIT.release()

    def route(self, head):
        self.response_started = False
        try:
            path = urlsplit(self.path).path
            if path == "/api/providers":
                from app.provider_registry import catalog, definition_errors

                return self.data(
                    json.dumps({"providers": catalog(), "errors": definition_errors()}),
                    "application/json",
                    head,
                )
            if path == "/health":
                return self.data('{"ok":true}', "application/json", head)
            if path.startswith("/stream/"):
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
                    return self.live(plan, row[0], head)
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
    """Repeat scans until shutdown, isolating failures between polling rounds."""
    while not STOP.is_set():
        try:
            sync()
        except Exception as error:
            LOG.warning("Refresh failed: %s", str(error)[:250])
        STOP.wait(CFG["interval_seconds"])


def main():
    """Load YAML, configure logging, and start the poller and HTTP server."""
    global CFG
    try:
        CFG = config.load(ROOT)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
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
