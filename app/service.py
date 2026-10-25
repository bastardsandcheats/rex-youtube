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
import sys
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
from app import discovery
from app import collections as rex_collections
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
PUBLISH_LOCK = threading.Lock()
PUBLISH_WAKE = threading.Event()
SCAN_WAKE = threading.Event()
DB_LOCAL = threading.local()
URLS = collections.OrderedDict()
FORMATS = {}
LIBRARIES = {}
ARTWORK = collections.OrderedDict()
CHECK_LIMIT = threading.BoundedSemaphore(2)


class ThreadDatabase:
    """Close the owned connection when a request/worker thread releases its local state."""

    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row

    def __del__(self):
        try:
            self.connection.close()
        except Exception:
            pass  # Interpreter shutdown may already have torn down SQLite.


@contextmanager
def db():
    """Reuse one connection per thread/path; commit each short operation.

    Closing the last WAL connection after every row forced repeated database
    reopening and checkpoints. Connections never cross threads, and changing
    ROOT (for isolated checks/tests) closes the old connection first.
    """
    path = ROOT / "data" / "metadata.db"
    if getattr(DB_LOCAL, "path", None) != path:
        previous = getattr(DB_LOCAL, "connection", None)
        if previous is not None:
            previous.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = ThreadDatabase(path)
        DB_LOCAL.path, DB_LOCAL.holder = path, holder
        DB_LOCAL.connection = holder.connection
    con = DB_LOCAL.connection
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise


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
        CREATE TABLE IF NOT EXISTS retired_versions(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS item_sources(source TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(source,key));
        CREATE TABLE IF NOT EXISTS failures(url TEXT PRIMARY KEY,checked REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS source_listings(
            source TEXT PRIMARY KEY, checked REAL NOT NULL, entries TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS items_status ON items(status);
        CREATE INDEX IF NOT EXISTS items_feed_id ON items(feed_id);
        CREATE INDEX IF NOT EXISTS items_pending_episode ON items(
            json_extract(metadata,'$.creator_id'), json_extract(metadata,'$.kind'),
            coalesce(json_extract(metadata,'$.episode'),0));
        CREATE INDEX IF NOT EXISTS items_episode_number ON items(
            json_extract(metadata,'$.creator_id'), json_extract(metadata,'$.provider'),
            coalesce(json_extract(metadata,'$.season'),cast(substr(json_extract(metadata,'$.published'),1,4) as integer)),
            json_extract(metadata,'$.library_name'), json_extract(metadata,'$.episode'));
        CREATE INDEX IF NOT EXISTS items_source_live ON items(
            json_extract(metadata,'$.source_entry'), json_extract(metadata,'$.is_live'));
        """)
        discovery.init_db(con)
        # Additive ownership bookkeeping keeps existing YAML/catalogs usable.
        con.execute(
            "INSERT OR IGNORE INTO item_sources SELECT json_extract(metadata,'$.source_entry'),key FROM items WHERE json_extract(metadata,'$.source_entry') IS NOT NULL"
        )


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


def catalog_rows():
    with db() as con:
        return [dict(row) for row in con.execute("SELECT * FROM items")]


def save_item(item, plan, source_entry):
    """Keep source identity, but give changed delivery layouts their own Rex version."""
    item = {
        **item,
        "media_info": playback.media_info(plan),
        "delivery": plan["kind"],
        "source_entry": source_entry,
        "checked": time.time(),
        "selection": playback.selection(plan),
        "size": plan["video"].get("filesize") if plan["kind"] == "http" else None,
    }
    if plan["kind"] == "http" and not item.get("size"):
        try:
            response = requests.head(
                plan["video"]["url"],
                headers=playback.headers(plan, plan["video"]),
                timeout=(5, 8),
            )
            length = response.headers.get("Content-Length", "")
            if (
                response.status_code == 200
                and length.isdigit()
                and not response.headers.get("Content-Encoding")
            ):
                item["size"] = int(length)
            response.close()
        except requests.RequestException:
            pass
    suffix = playback.extension(plan)
    digest = hashlib.sha256(item["key"].encode()).hexdigest()[:24]
    directory = item["provider"] + (
        "-live" if item["is_live"] else "-movies" if item["kind"] == "movie" else ""
    )
    with db() as con:
        previous = con.execute(
            "SELECT * FROM items WHERE key=?", (item["key"],)
        ).fetchone()
        old = json.loads(previous["metadata"]) if previous else {}
        item["native_display"] = True
        if old and not old.get("native_display"):
            item["legacy_display"] = old.get("legacy_display", old)
        same_group = (old.get("kind"), old.get("creator_id"), old.get("season")) == (
            item["kind"],
            item["creator_id"],
            item.get("season"),
        )
        item["episode"] = old.get("episode", 0) if same_group else 0
        # A refresh lacking optional metadata must not erase an existing description/artwork.
        for field in ("description", "thumbnail", "creator_description", "creator_art"):
            if not item.get(field) and old.get(field):
                item[field] = old[field]
        changed = previous and playback.delivery_changed(old, item)
        version = hashlib.sha256(
            json.dumps(item["selection"], sort_keys=True).encode()
        ).hexdigest()[:16]
        feed_id = (
            item["key"] + ":" + version
            if changed
            else previous["feed_id"] if previous else item["key"]
        )
        path = (
            f"{directory}/{digest}-{version}.{suffix}"
            if changed
            else (
                previous["relay_path"] if previous else f"{directory}/{digest}.{suffix}"
            )
        )
        if changed:
            con.execute(
                "INSERT OR IGNORE INTO retired_versions VALUES(?)",
                (previous["feed_id"],),
            )
        status = "probing" if item.get("probe") is True else "ready"
        con.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET metadata=excluded.metadata,feed_id=excluded.feed_id,relay_path=excluded.relay_path,status=excluded.status,rating_key=CASE WHEN items.feed_id=excluded.feed_id THEN items.rating_key ELSE NULL END",
            (item["key"], json.dumps(item), feed_id, path, None, status),
        )
        con.execute(
            "INSERT OR IGNORE INTO item_sources VALUES(?,?)",
            (source_entry, item["key"]),
        )
    with LOCK:
        FORMATS[item["key"]] = (time.time(), plan)
    reorder(item["creator_id"])
    if status == "probing":
        try:
            # Probe the actual rewritten relay, not an upstream format we do not serve.
            measured = playback.full_probe(local_url() + "/stream/" + path)
            if item["is_live"]:
                # Some probes describe only the current DVR/playlist window.
                # Declaring that as the length makes Rex stop a live broadcast.
                measured.get("format", {}).pop("duration", None)
            with db() as con:
                current = json.loads(
                    con.execute(
                        "SELECT metadata FROM items WHERE key=?", (item["key"],)
                    ).fetchone()[0]
                )
                current["media_info"] = measured
                if previous and playback.stream_layout(
                    old.get("media_info", {})
                ) != playback.stream_layout(measured):
                    # Correcting an inferred probe also needs a new Rex media version.
                    measured_version = hashlib.sha256(
                        json.dumps(
                            [item["selection"], playback.stream_layout(measured)],
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()[:16]
                    measured_id = item["key"] + ":" + measured_version
                    measured_path = f"{directory}/{digest}-{measured_version}.{suffix}"
                    con.execute(
                        "INSERT OR IGNORE INTO retired_versions VALUES(?)",
                        (previous["feed_id"],),
                    )
                    con.execute(
                        "UPDATE items SET feed_id=?,relay_path=?,rating_key=NULL WHERE key=?",
                        (measured_id, measured_path, item["key"]),
                    )
                if (
                    current["delivery"] == "http"
                    and str(measured.get("format", {}).get("size", "")).isdigit()
                ):
                    current["size"] = int(measured["format"]["size"])
                con.execute(
                    "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                    (json.dumps(current), item["key"]),
                )
        except Exception:
            with db() as con:
                con.execute(
                    "UPDATE items SET status='error' WHERE key=?", (item["key"],)
                )
            raise

    PUBLISH_WAKE.set()


def reorder(creator_id):
    """Assign numbers only to new placements. Backfills never renumber existing uploads."""
    with db() as con:
        # The expression index excludes all already-numbered rows before JSON decoding.
        rows = [
            (r, json.loads(r["metadata"]))
            for r in con.execute(
                "SELECT * FROM items WHERE json_extract(metadata,'$.creator_id')=? "
                "AND json_extract(metadata,'$.kind')='episode' "
                "AND coalesce(json_extract(metadata,'$.episode'),0)=0",
                (creator_id,),
            )
        ]
        groups = collections.defaultdict(list)
        for row, item in rows:
            if item["kind"] == "episode" and item["creator_id"] == creator_id:
                groups[
                    (
                        item["provider"],
                        item.get("season", int(item["published"][:4] or 0)),
                        item.get("library_name"),
                    )
                ].append((row, item))
        for (provider, season, library), members in groups.items():
            # Indexed MAX reads the last assigned number without loading the channel.
            number = con.execute(
                "SELECT coalesce(max(json_extract(metadata,'$.episode')),0) FROM items "
                "WHERE json_extract(metadata,'$.creator_id')=? "
                "AND json_extract(metadata,'$.provider')=? "
                "AND coalesce(json_extract(metadata,'$.season'),cast(substr(json_extract(metadata,'$.published'),1,4) as integer))=? "
                "AND json_extract(metadata,'$.library_name') IS ?",
                (creator_id, provider, season, library),
            ).fetchone()[0]
            for row, item in sorted(
                members,
                key=lambda r: (
                    r[1]["published"],
                    r[1].get("timestamp", 0),
                    r[1]["key"],
                ),
            ):
                if item.get("episode", 0):
                    continue
                number += 1
                item["episode"] = number
                con.execute(
                    "UPDATE items SET metadata=? WHERE key=?",
                    (json.dumps(item), row["key"]),
                )


def feed_item(row):
    """Source display is metadata, not a locked user edit: Rex respects the user's changes."""
    item = json.loads(row["metadata"])
    display = {"title": item["title"]}
    for source, target in (
        ("description", "summary"),
        ("published", "date"),
        ("thumbnail", "thumb"),
    ):
        if item.get(source):
            display[target] = item[source]
    if item["kind"] == "episode":
        show = {"title": item["creator_name"]}
        if item.get("creator_description"):
            show["summary"] = item["creator_description"]
        if item.get("creator_art"):
            show["poster"] = item["creator_art"]
            display["season"] = {"poster": item["creator_art"]}
        display["show"] = show
    # HLS now has its real extension; DASH still uses a logical video file label.
    file = "/rex-youtube/" + row["relay_path"]
    if item["delivery"] == "dash":
        file = file.rsplit(".", 1)[0] + ".mp4"
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
        "display": display,
    }
    if item["kind"] == "episode":
        payload.update(
            season=item.get("season", int(item["published"][:4] or 0)),
            episode=item["episode"],
        )
    if item.get("size") and item["delivery"] == "http":
        payload["size"] = item["size"]
    return payload


def retire_versions():
    """Retry version withdrawals after a crash; new versions have distinct file labels."""
    with db() as con:
        if not con.execute("SELECT 1 FROM retired_versions LIMIT 1").fetchone():
            return
        con.execute(
            "DELETE FROM retired_versions WHERE EXISTS "
            "(SELECT 1 FROM items WHERE feed_id=retired_versions.id)"
        )
        ids = [r[0] for r in con.execute("SELECT id FROM retired_versions")]
    for offset in range(0, len(ids), 200):
        chunk = ids[offset : offset + 200]
        rex("DELETE", "/rex/feed/" + CFG["source"] + "/items", {"ids": chunk})
        with db() as con:
            con.executemany(
                "DELETE FROM retired_versions WHERE id=?", [(key,) for key in chunk]
            )


def release_source_locks(row, item):
    """Migrate old source-created scalar locks only when the value still matches our cache.

    Differing user edits and uploaded posters stay untouched. New feed display never locks.
    """
    old = item.get("legacy_display")
    if not old and item.get("native_display"):
        return
    old = old or item
    if not row.get("rating_key"):
        return
    leaf = rex_collections.entries(rex("GET", "/library/metadata/" + row["rating_key"]))
    if not leaf:
        return
    fields = {
        "title": old["title"],
        "summary": old.get("description"),
        "originallyAvailableAt": old.get("published"),
    }
    targets = [(leaf[0], fields, 4 if old["kind"] == "episode" else 1)]
    if old["kind"] == "episode" and leaf[0].get("grandparentRatingKey"):
        parents = rex_collections.entries(
            rex("GET", "/library/metadata/" + str(leaf[0]["grandparentRatingKey"]))
        )
        if parents:
            targets.append(
                (
                    parents[0],
                    {
                        "title": old["creator_name"],
                        "summary": old.get("creator_description"),
                    },
                    2,
                )
            )
    for remote, expected, kind in targets:
        params = {
            field + ".locked": 0
            for field, value in expected.items()
            if value and remote.get(field) == value
        }
        if params:
            params["type"] = kind
            rex("PUT", "/library/metadata/" + str(remote["ratingKey"]), params=params)


def publish(row):
    """Publish a single row for callers outside the background queue."""
    publish_batch([row])


def publish_batch(rows):
    """One upsert and receipt listing per bounded batch, not per video.

    Only confirmed, active receipts complete local work. A failed or partial
    response leaves unconfirmed rows queued for an idempotent retry.
    """
    for row in rows:
        release_source_locks(row, json.loads(row["metadata"]))
    response = rex(
        "POST",
        "/rex/feed/" + CFG["source"] + "/items",
        {"items": [feed_item(row) for row in rows]},
    )
    if response.get("rejected") or response.get("held"):
        raise RuntimeError("Rex refused media metadata in the publication batch")
    receipts = {
        row["feed_id"]: {"ratingKey": row["rating_key"]}
        for row in rows
        if row.get("rating_key")
    }
    if any(row["feed_id"] not in receipts for row in rows):
        receipts.update(
            {
                receipt["id"]: receipt
                for receipt in rex("GET", "/rex/feed/" + CFG["source"] + "/items")[
                    "items"
                ]
                if not receipt.get("deleted") and receipt.get("ratingKey")
            }
        )
    confirmed = []
    with db() as con:
        for row in rows:
            receipt = receipts.get(row["feed_id"])
            if receipt is None:
                continue
            item = json.loads(row["metadata"])
            item["native_display"] = True
            item.pop("legacy_display", None)
            con.execute(
                "UPDATE items SET rating_key=?,status='published',metadata=? WHERE key=?",
                (str(receipt["ratingKey"]), json.dumps(item), row["key"]),
            )
            confirmed.append(row["key"])
    LOG.info("Published batch: %d/%d items", len(confirmed), len(rows))
    if len(confirmed) != len(rows):
        raise RuntimeError(
            "Rex publication receipts missing; unconfirmed items remain queued"
        )


def flush_ready():
    """Serialize delivery shared by the scanner and background publisher."""
    with PUBLISH_LOCK:
        return deliver_ready()


def deliver_ready():
    """Deliver pending catalog rows without letting one failure stop the rest."""
    with db() as con:
        rows = [
            dict(r) for r in con.execute("SELECT * FROM items WHERE status='ready'")
        ]
    success = True
    for offset in range(0, len(rows), 100):
        if STOP.is_set():
            return False
        batch = rows[offset : offset + 100]
        try:
            publish_batch(batch)
        except Exception as error:
            success = False
            LOG.warning(
                "Delivery batch failed (%d items): %s", len(batch), type(error).__name__
            )
    if success:
        retire_versions()
    return success


def remove_offline(source_entry):
    """Only a positive offline result may remove a transient live movie."""
    with db() as con:
        rows = [
            (r, json.loads(r["metadata"]))
            for r in con.execute(
                "SELECT * FROM items WHERE json_extract(metadata,'$.source_entry')=? "
                "AND json_extract(metadata,'$.is_live')=1",
                (source_entry,),
            )
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


def background_call(call, *args, **kwargs):
    """Average a single scanner thread's CPU use without delaying playback.

    yt-dlp calls are not preempted: their measured thread CPU cost determines
    an interruptible pause afterwards. Network waiting already counts as yield.
    """
    started, cpu = time.monotonic(), time.thread_time()
    try:
        return call(*args, **kwargs)
    finally:
        target = CFG.get("scan_cpu_percent", 20) / 100
        pause = (time.thread_time() - cpu) / target - (time.monotonic() - started)
        if pause > 0:
            STOP.wait(pause)


def source_listing(url, settings):
    """Reuse a channel listing while a bounded metadata backfill catches up.

    The cache is durable so restarts do not re-enumerate thousands of uploads.
    Live targets always query upstream. Only successfully exhausted listings are
    cached; extraction failures never replace an earlier successful inventory.
    """
    cacheable = (
        providers.detect(url) == "youtube"
        and "/@" in url
        and not url.rstrip("/").endswith("/live")
        and settings.get("type") != "live"
    )
    identity = json.dumps([url, settings["max_items"]])
    ttl = max(3600, settings.get("interval_seconds", 0))
    if cacheable:
        with db() as con:
            cached = con.execute(
                "SELECT entries FROM source_listings WHERE source=? AND checked>?",
                (identity, time.time() - ttl),
            ).fetchone()
        if cached:
            return json.loads(cached[0])
    entries = background_call(
        lambda: list(providers.enumerate_source(url, limit=settings["max_items"]))
    )
    if cacheable and entries:
        with db() as con:
            con.execute(
                "INSERT OR REPLACE INTO source_listings VALUES(?,?,?)",
                (identity, time.time(), json.dumps(entries)),
            )
    return entries


def sync_source(url, settings, budget=None, *, entries=None, force=False, prune=True):
    """Refresh a bounded batch, retaining cached items outside a capped listing window."""
    entries = source_listing(url, settings) if entries is None else entries
    count = 0
    complete = bool(entries)
    seen = set()
    for webpage, context, entry in entries:
        if STOP.is_set():
            return False
        source = providers.provider(
            entry.get("ie_key") or entry.get("extractor_key") or context["provider"]
        )
        key = f'{source}:video:{entry.get("id")}'
        with db() as con:
            old = con.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
            failure = con.execute(
                "SELECT checked FROM failures WHERE url=?", (webpage,)
            ).fetchone()
        seen.add(key)
        if old:
            previous = json.loads(old["metadata"])
            updated = providers.apply_settings(previous, settings)
            if not previous.get("native_display"):
                updated["legacy_display"] = previous.get("legacy_display", previous)
            if (
                previous.get("kind"),
                previous.get("season"),
                previous.get("creator_id"),
                previous.get("library_name"),
            ) != (
                updated.get("kind"),
                updated.get("season"),
                updated.get("creator_id"),
                updated.get("library_name"),
            ):
                updated["episode"] = 0
            visible_change = any(
                updated.get(field) != previous.get(field)
                for field in updated.keys() | previous.keys()
                if field != "refresh_hours"
            )
            if visible_change:
                with db() as con:
                    con.execute(
                        "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                        (json.dumps(updated), key),
                    )
                reorder(updated["creator_id"])
                PUBLISH_WAKE.set()
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO item_sources VALUES(?,?)", (url, key)
                )
            refresh_hours = settings.get("refresh_hours", 24)
            due = bool(refresh_hours) and (
                time.time() - previous.get("checked", 0) >= refresh_hours * 3600
            )
            # Channel listings already expose changed titles. A disabled periodic
            # refresh must still notice those and actively check live broadcasts.
            title_changed = bool(entry.get("title")) and " ".join(
                entry["title"].split()
            ) != " ".join(
                str(previous.get("source_title", previous.get("title", ""))).split()
            )
            needs_extract = (
                force
                or due
                or title_changed
                or bool(previous.get("is_live"))
                or previous.get("max_height", CFG["max_height"])
                != settings.get("max_height", CFG["max_height"])
                or previous.get("probe", "headers") != settings.get("probe", "headers")
                or old["status"] in ("error", "probing")
            )
            if not needs_extract:
                continue
        if (
            failure
            and not old
            and time.time() - failure[0] < 86400
            and not entry.get("is_live")
        ):
            complete = False
            continue
        if count >= settings.get("batch_size", CFG["batch_size"]):
            # Cached items are still valid; unindexed items mean this is not a full scan.
            complete = complete and old is not None
            continue
        # Bound the entire upload pass, not just each of many large channels.
        # Live status checks are independent of the upload backfill quota.
        if budget is not None and settings.get("type") != "live":
            if budget["remaining"] <= 0:
                complete = False
                continue
            budget["remaining"] -= 1
        count += 1
        try:
            info = background_call(providers.extract, webpage)
            if entry.get("_since"):
                published = discovery.date(
                    info.get("upload_date") or info.get("release_date")
                )
                if not published:
                    complete = False
                    continue
                if published < entry["_since"]:
                    continue  # An exact date is required before publishing a year-limited upload.
            item = providers.apply_settings(
                providers.normalize(info, context), settings, context
            )
            if settings.get("type") == "live" and not item["is_live"]:
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
                complete = False
                continue
            plan = playback.choose(info, settings.get("max_height", CFG["max_height"]))
            if settings.get("probe", "headers") == "headers":
                playback.inspect_layout(plan)
            save_item(item, plan, url)
            seen.add(item["key"])
            with db() as con:
                con.execute("DELETE FROM failures WHERE url=?", (webpage,))
        except providers.Unavailable:
            remove_offline(url)
        except Exception as error:
            complete = False
            LOG.warning("Cannot index %s: %s", source, type(error).__name__)
            reason = discovery.restriction(error) or type(error).__name__
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO failures VALUES(?,?,?)",
                    (webpage, time.time(), reason),
                )
    # Only a complete, uncapped listing can prove that an old upload disappeared.
    # An empty result is deliberately not taken as authority to remove everything.
    if (
        prune
        and complete
        and entries
        and (not settings["max_items"] or len(entries) < settings["max_items"])
    ):
        with db() as con:
            old_keys = [
                r[0]
                for r in con.execute(
                    "SELECT key FROM item_sources WHERE source=?", (url,)
                )
            ]
            con.executemany(
                "DELETE FROM item_sources WHERE source=? AND key=?",
                [(url, key) for key in old_keys if key not in seen],
            )
    return complete


def reconcile_sources(active):
    """Prune only after every configured source was listed successfully this round."""
    with db() as con:
        sources = [
            r[0] for r in con.execute("SELECT DISTINCT source FROM item_sources")
        ]
        con.executemany(
            "DELETE FROM item_sources WHERE source=?",
            [(url,) for url in sources if url not in active],
        )
        unused = list(
            con.execute(
                "SELECT key,feed_id FROM items WHERE key NOT IN (SELECT key FROM item_sources)"
            )
        )
        con.executemany(
            "INSERT OR IGNORE INTO retired_versions VALUES(?)",
            [(row["feed_id"],) for row in unused],
        )
        con.executemany(
            "DELETE FROM items WHERE key=?", [(row["key"],) for row in unused]
        )


def snapshot():
    """Send complete cached inventory in chunks; restart a lost session at part zero."""
    rows = catalog_rows()
    if any(row["status"] != "published" for row in rows):
        return False
    items = [feed_item(row) for row in rows]
    for attempt in range(2):
        session = str(time.time_ns())
        chunks = [items[n : n + 200] for n in range(0, len(items), 200)] or [[]]
        try:
            for part, chunk in enumerate(chunks):
                result = rex(
                    "PUT",
                    "/rex/feed/" + CFG["source"] + "/snapshot",
                    {
                        "session": session,
                        "part": part,
                        "complete": part == len(chunks) - 1,
                        "items": chunk,
                    },
                )
                if result.get("rejected") or result.get("held"):
                    raise ValueError("Rex refused snapshot metadata")
            state("last_snapshot", time.time())
            return True
        except requests.HTTPError as error:
            response = error.response
            if (
                attempt == 0
                and response is not None
                and response.status_code == 409
                and "no such snapshot session" in response.text.lower()
            ):
                continue
            # Never bypass the server's shrink guard with force.
            raise


def sync():
    """Refresh metadata, reconcile ownership and publish only complete snapshots."""
    runtime = sys.modules[__name__]
    discovery.seed(runtime)
    targets = [
        target
        for entry in config.source_entries(CFG)
        for target in providers.configured_targets(entry)
    ]
    channels = {
        target["url"]: target["options"]
        for target in targets
        if discovery.channel(target["url"], target["options"])
    }
    due = {
        target["url"]
        for target in targets
        if time.time() - float(state("source_poll:" + target["url"]) or 0)
        >= target["options"].get("interval_seconds", 0)
    }
    snapshot_due = (
        time.time() - float(state("last_snapshot") or 0)
        >= CFG.get("snapshot_hours", 6) * 3600
    )
    with db() as con:
        pending = con.execute(
            "SELECT 1 FROM items WHERE status='ready' "
            "UNION ALL SELECT 1 FROM retired_versions LIMIT 1"
        ).fetchone()
    if (
        not due
        and not pending
        and not snapshot_due
        and not discovery.pending(runtime, channels)
    ):
        return  # No registration, catalog decoding, HTTP calls or collection work.
    setup_rex()
    complete = flush_ready()
    active = set()
    budget = {"remaining": CFG["max_extracts"]} if CFG.get("max_extracts") else None
    for entry in config.source_entries(CFG):
        for target in providers.configured_targets(entry):
            url = target["url"]
            active.add(url)
            # Persist per-source schedules across restarts; failed/partial rounds retry.
            interval = target["options"].get("interval_seconds", 0)
            last_poll = float(state("source_poll:" + url) or 0)
            if interval and time.time() - last_poll < interval:
                continue
            try:
                source_complete = (
                    discovery.discover(runtime, url, target["options"])
                    if url in channels
                    else sync_source(url, target["options"], budget)
                )
                if source_complete:
                    state("source_poll:" + url, time.time())
                complete = source_complete and complete
            except providers.Unavailable:
                remove_offline(url)
            except Exception as error:
                complete = False
                LOG.warning("Source check failed: %s", type(error).__name__)
    discovery.process(runtime, channels)
    if complete:
        reconcile_sources(active)
    complete = flush_ready() and complete
    # Partial rounds may add known memberships, never remove them.
    try:
        rex_collections.reconcile(catalog_rows(), rex, db, prune=complete)
    except Exception as error:
        LOG.warning("Collection reconciliation failed: %s", type(error).__name__)
    if complete:
        if (
            time.time() - float(state("last_snapshot") or 0)
            >= CFG.get("snapshot_hours", 6) * 3600
        ):
            snapshot()


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
        try:
            plan = playback.choose_pinned(
                info, item.get("max_height", CFG["max_height"]), item.get("selection")
            )
            if playback.delivery_changed(
                item,
                {
                    "delivery": plan["kind"],
                    "media_info": playback.media_info(plan),
                    "selection": playback.selection(plan),
                },
            ):
                raise ValueError("Published media layout changed")
        except ValueError:
            # Refuse mismatched bytes, but explicitly queue this item's refresh.
            # Incremental discovery no longer re-extracts the whole archive by age.
            with db() as con:
                item["checked"] = 0
                con.execute(
                    "UPDATE items SET metadata=? WHERE key=?", (json.dumps(item), key)
                )
                queued = con.execute(
                    "UPDATE upload_inventory SET status='pending',origin='new',retry_after=0 WHERE key=?",
                    (key,),
                ).rowcount
            if queued:
                SCAN_WAKE.set()
            raise
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
            if path == "/api/discovery":
                return self.data(
                    json.dumps({"jobs": discovery.stats(sys.modules[__name__])}),
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


def publisher():
    """Sleep until delivery is requested; only failed delivery gets a timed retry."""
    retry_seconds = None
    while not STOP.is_set():
        PUBLISH_WAKE.wait(retry_seconds)
        PUBLISH_WAKE.clear()
        if STOP.is_set():
            return
        try:
            retry_seconds = None if flush_ready() else 300
        except Exception as error:
            retry_seconds = 300
            LOG.warning("Background delivery failed: %s", type(error).__name__)


def worker():
    """Honor the durable next scan time, including across service restarts."""
    while not STOP.is_set():
        delay = max(0, float(state("next_scan") or 0) - time.time())
        if delay:
            SCAN_WAKE.wait(delay)
            SCAN_WAKE.clear()
            if STOP.is_set():
                return
        state("next_scan", time.time() + CFG["interval_seconds"])
        started = time.monotonic()
        LOG.info("Scan started")
        try:
            sync()
        except Exception as error:
            LOG.warning("Refresh failed: %s", str(error)[:250])
        state("next_scan", time.time() + CFG["interval_seconds"])
        LOG.info(
            "Scan finished in %.1fs; next scan in %ds",
            time.monotonic() - started,
            CFG["interval_seconds"],
        )


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
    PUBLISH_WAKE.set()  # Recover durable pending deliveries once at startup.
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    server.daemon_threads = True
    threading.Thread(target=worker, daemon=True, name="source-sync").start()
    threading.Thread(target=publisher, daemon=True, name="feed-publisher").start()
    LOG.info(
        "rex-youtube listening on port %s; metadata only, no media cache", CFG["port"]
    )
    try:
        server.serve_forever()
    finally:
        STOP.set()
        SCAN_WAKE.set()
        PUBLISH_WAKE.set()
        server.server_close()


if __name__ == "__main__":
    main()
