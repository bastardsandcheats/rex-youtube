"""Incremental YouTube discovery and durable, separate metadata backfill.

A discovered ID is a cursor boundary, not a successful metadata job. Failed jobs
stay in the queue; stopping a listing never implies removal of older media.
"""

import json
import time
from datetime import datetime, timezone

RESTRICTED = {"subscriber_only", "premium_only", "private", "needs_auth"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_sources (
    source TEXT PRIMARY KEY, since TEXT NOT NULL, full_checked REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS upload_inventory (
    source TEXT NOT NULL, key TEXT NOT NULL, webpage TEXT NOT NULL,
    context TEXT NOT NULL, entry TEXT NOT NULL, status TEXT NOT NULL,
    origin TEXT NOT NULL, retry_after REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(source,key));
CREATE INDEX IF NOT EXISTS upload_jobs ON upload_inventory(status,retry_after,origin);
"""


def restriction(error):
    """Persist a safe category, never a signed URL or raw provider response."""
    text = str(error).lower()
    if "members-only" in text or "join this channel" in text:
        return "subscriber_only"
    if "private video" in text:
        return "private"
    if "confirm your age" in text or "age-restricted" in text:
        return "needs_auth"
    return None


def channel(url, settings):
    return (
        "youtube.com/" in url
        and ("/@" in url or "/channel/" in url)
        and not url.rstrip("/").endswith("/live")
        and settings.get("type") != "live"
    )


def since_year():
    return datetime.now(timezone.utc).strftime("%Y0101")


def date(value):
    value = str(value or "").replace("-", "")[:8]
    return value if len(value) == 8 and value.isdigit() else ""


def init_db(con):
    con.executescript(SCHEMA)


def seed(s):
    """Migrate cached inventories once, without fetching or re-extracting videos."""
    if s.state("incremental_inventory_v1"):
        return
    with s.db() as con:
        cached = list(con.execute("SELECT source,checked,entries FROM source_listings"))
        for row in cached:
            url, _ = json.loads(row["source"])
            if not channel(url, {}):
                continue
            cutoff = since_year()
            con.execute(
                "INSERT OR IGNORE INTO upload_sources VALUES(?,?,?)",
                (url, cutoff, row["checked"]),
            )
            for webpage, context, entry in json.loads(row["entries"]):
                key = "youtube:video:" + str(entry.get("id"))
                existing = con.execute(
                    "SELECT metadata FROM items WHERE key=?", (key,)
                ).fetchone()
                if existing:
                    entry = dict(
                        entry,
                        upload_date=date(json.loads(existing[0]).get("published")),
                    )
                published = date(entry.get("upload_date"))
                status = (
                    "ignored"
                    if published and published < cutoff
                    else "done" if existing else "pending"
                )
                con.execute(
                    "INSERT OR IGNORE INTO upload_inventory VALUES(?,?,?,?,?,?,?,0)",
                    (
                        url,
                        key,
                        webpage,
                        json.dumps(context),
                        json.dumps(entry),
                        status,
                        "backfill",
                    ),
                )
    s.state("incremental_inventory_v1", time.time())


def apply_policy(s, url, settings):
    """Apply changed YAML once, without reopening every video on each poll."""
    fields = (
        "type",
        "name",
        "year_seasons",
        "season",
        "library",
        "movie_library",
        "live_library",
        "collection",
        "collection_type",
        "probe",
        "max_height",
    )
    signature = json.dumps(
        {field: settings.get(field) for field in fields}, sort_keys=True
    )
    state_key = "upload_policy:" + url
    if s.state(state_key) == signature:
        return
    creators = set()
    with s.db() as con:
        rows = list(
            con.execute(
                "SELECT i.* FROM items i JOIN item_sources o ON i.key=o.key WHERE o.source=?",
                (url,),
            )
        )
        for row in rows:
            previous = json.loads(row["metadata"])
            updated = s.providers.apply_settings(previous, settings)
            changed = any(
                updated.get(k) != previous.get(k)
                for k in updated.keys() | previous.keys()
                if k != "refresh_hours"
            )
            if not changed:
                continue
            if (
                updated["kind"],
                updated["creator_id"],
                updated.get("season"),
                updated.get("library_name"),
            ) != (
                previous["kind"],
                previous["creator_id"],
                previous.get("season"),
                previous.get("library_name"),
            ):
                updated["episode"] = 0
                creators.add(updated["creator_id"])
            con.execute(
                "UPDATE items SET metadata=?,status='ready' WHERE key=?",
                (json.dumps(updated), row["key"]),
            )
            if any(updated.get(k) != previous.get(k) for k in ("max_height", "probe")):
                con.execute(
                    "UPDATE upload_inventory SET status='pending',origin='new',retry_after=0 WHERE source=? AND key=?",
                    (url, row["key"]),
                )
    for creator in creators:
        s.reorder(creator)
    if rows:
        s.PUBLISH_WAKE.set()
    s.state(state_key, signature)


def discover(s, url, settings):
    """Read until overlap with known IDs, or the channel's Jan-1 boundary.

    A weekly reconciliation reads through the year boundary again, detecting
    delayed/reordered uploads. Neither incremental nor reconciliation passes
    delete unseen media; removal is an explicit operation.
    """
    apply_policy(s, url, settings)
    with s.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO upload_sources(source,since) VALUES(?,?)",
            (url, since_year()),
        )
        source = con.execute(
            "SELECT * FROM upload_sources WHERE source=?", (url,)
        ).fetchone()
        known = {
            r[0]
            for r in con.execute(
                "SELECT key FROM upload_inventory WHERE source=?", (url,)
            )
        }
    cutoff = source["since"]
    full = time.time() - source["full_checked"] >= 7 * 86400
    cutoff_stamp = (
        datetime.strptime(cutoff, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    )
    overlap = old = inspected = queued = 0
    seen = set()
    iterator = s.providers.iter_youtube_uploads(url, settings["max_items"])
    try:
        for webpage, context, entry in iterator:
            if s.STOP.is_set():
                return False
            inspected += 1
            key = "youtube:video:" + str(entry["id"])
            # Relative listing dates can be imprecise near New Year. Carry a
            # two-month overlap; exact dates gate actual publication below.
            timestamp = entry.get("timestamp")
            exact = date(entry.get("upload_date"))
            definitely_old = bool(exact and exact < cutoff) or bool(
                timestamp and timestamp < cutoff_stamp - 62 * 86400
            )
            old = old + 1 if definitely_old else 0
            overlap = overlap + 1 if key in known else 0
            if old >= 3 or (not full and overlap >= 3):
                break
            seen.add(key)
            with s.db() as con:
                existing = con.execute(
                    "SELECT metadata FROM items WHERE key=?", (key,)
                ).fetchone()
                if existing:
                    published = date(json.loads(existing[0]).get("published"))
                    status = "ignored" if published and published < cutoff else "done"
                    if status == "done":
                        con.execute(
                            "INSERT OR IGNORE INTO item_sources VALUES(?,?)", (url, key)
                        )
                else:
                    status = (
                        "ignored"
                        if definitely_old
                        else (
                            "restricted"
                            if entry.get("availability") in RESTRICTED
                            else "pending"
                        )
                    )
                result = con.execute(
                    "INSERT OR IGNORE INTO upload_inventory VALUES(?,?,?,?,?,?,?,0)",
                    (
                        url,
                        key,
                        webpage,
                        json.dumps(context),
                        json.dumps(entry),
                        status,
                        "backfill" if full else "new",
                    ),
                )
                if not existing:
                    con.execute(
                        "UPDATE upload_inventory SET entry=?,context=?,status=CASE WHEN ? THEN 'ignored' WHEN ? THEN 'restricted' WHEN status='restricted' AND ? THEN 'pending' ELSE status END WHERE source=? AND key=?",
                        (
                            json.dumps(entry),
                            json.dumps(context),
                            definitely_old,
                            entry.get("availability") in RESTRICTED,
                            entry.get("availability") == "public",
                            url,
                            key,
                        ),
                    )
                if existing and status == "done" and entry.get("title"):
                    saved = json.loads(existing[0])
                    changed = " ".join(entry["title"].split()) != " ".join(
                        str(saved.get("source_title", saved.get("title", ""))).split()
                    )
                    if changed:
                        con.execute(
                            "UPDATE upload_inventory SET status='pending',origin='new',entry=?,context=? WHERE source=? AND key=? AND entry!=?",
                            (
                                json.dumps(entry),
                                json.dumps(context),
                                url,
                                key,
                                json.dumps(entry),
                            ),
                        )
                queued += bool(result.rowcount and status == "pending")
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    if not inspected:
        return False
    if full:
        with s.db() as con:
            # A complete year-window pass can discard legacy *queued work*
            # outside that window. Existing media is deliberately retained.
            stale = [
                r[0]
                for r in con.execute(
                    "SELECT key FROM upload_inventory WHERE source=? AND origin='backfill' AND status='pending'",
                    (url,),
                )
                if r[0] not in seen
            ]
            con.executemany(
                "UPDATE upload_inventory SET status='ignored' WHERE source=? AND key=?",
                [(url, key) for key in stale],
            )
            con.execute(
                "UPDATE upload_sources SET full_checked=? WHERE source=?",
                (time.time(), url),
            )
    s.LOG.info(
        "Discovery: %d listing entries inspected, %d new metadata jobs; %s",
        inspected,
        queued,
        "year reconciliation" if full else "known-ID overlap",
    )
    return True


def pending(s, sources):
    if not sources:
        return False
    with s.db() as con:
        return bool(
            con.execute(
                "SELECT 1 FROM upload_inventory WHERE status='pending' AND retry_after<=? AND source IN (%s) LIMIT 1"
                % ",".join("?" for _ in sources),
                [time.time(), *sources],
            ).fetchone()
        )


def classify_restrictions(s):
    """Flat listings can identify members-only items without opening the video."""
    with s.db() as con:
        con.execute(
            "UPDATE upload_inventory SET status='restricted',retry_after=0 "
            "WHERE status='pending' AND json_extract(entry,'$.availability') IN "
            "('subscriber_only','premium_only','private','needs_auth')"
        )


def process(s, sources):
    """Process all newly discovered IDs; historical work has its own batch/schedule."""
    if not sources:
        return
    classify_restrictions(s)
    history_due = time.time() - float(s.state("upload_backfill_checked") or 0) >= 3600
    with s.db() as con:
        rows = list(
            con.execute(
                "SELECT q.*,s.since FROM upload_inventory q JOIN upload_sources s USING(source) "
                "WHERE q.status='pending' AND q.retry_after<=? AND q.source IN (%s) "
                "ORDER BY CASE q.origin WHEN 'new' THEN 0 ELSE 1 END,q.rowid"
                % ",".join("?" for _ in sources),
                [time.time(), *sources],
            )
        )
    historical = 0
    for row in rows:
        if s.STOP.is_set():
            return
        if row["origin"] == "backfill":
            if not history_due or historical >= s.CFG.get("backfill_batch_size", 25):
                continue
            historical += 1
        settings = dict(sources[row["source"]], batch_size=1, refresh_hours=0)
        entry = json.loads(row["entry"])
        if entry.get("availability") in RESTRICTED:
            with s.db() as con:
                con.execute(
                    "UPDATE upload_inventory SET status='restricted',retry_after=0 WHERE source=? AND key=?",
                    (row["source"], row["key"]),
                )
            continue
        entry["_since"] = row["since"]
        ok = s.sync_source(
            row["source"],
            settings,
            entries=[(row["webpage"], json.loads(row["context"]), entry)],
            force=True,
            prune=False,
        )
        with s.db() as con:
            item = con.execute(
                "SELECT metadata FROM items WHERE key=?", (row["key"],)
            ).fetchone()
            if ok and item:
                published = date(json.loads(item[0]).get("published"))
                status = (
                    "done" if published and published >= row["since"] else "ignored"
                )
            elif ok:
                status = (
                    "ignored"  # Exact extraction proved the item predates the boundary.
                )
            else:
                failure = con.execute(
                    "SELECT error FROM failures WHERE url=?", (row["webpage"],)
                ).fetchone()
                status = (
                    "restricted" if failure and failure[0] in RESTRICTED else "pending"
                )
            con.execute(
                "UPDATE upload_inventory SET status=?,retry_after=? WHERE source=? AND key=?",
                (
                    status,
                    0 if status != "pending" else time.time() + 86400,
                    row["source"],
                    row["key"],
                ),
            )
    if historical:
        s.state("upload_backfill_checked", time.time())


def stats(s):
    with s.db() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT origin,status,count(*) AS count FROM upload_inventory GROUP BY origin,status"
            )
        ]
