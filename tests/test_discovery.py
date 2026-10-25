"""Incremental paging must stop network iteration, retain retries and respect year boundaries."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from app import service as s, discovery, config
from fixtures import info


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(s, "ROOT", Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        self.cfg = patch.dict(s.CFG, config.normalize({}), clear=True)
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        s.init_db()
        self.url = "https://www.youtube.com/@Example"
        self.opts = dict(config.DEFAULT_OPTIONS, max_items=0, refresh_hours=0)

    def source(self, full=True):
        with s.db() as con:
            con.execute(
                "INSERT INTO upload_sources VALUES(?,?,?)",
                (self.url, "20260101", time.time() if full else 0),
            )

    def known(self, ident, origin="backfill", status="done"):
        with s.db() as con:
            con.execute(
                "INSERT INTO upload_inventory VALUES(?,?,?,?,?,?,?,0)",
                (
                    self.url,
                    "youtube:video:" + ident,
                    "https://youtube.com/watch?v=" + ident,
                    json.dumps({"provider": "youtube"}),
                    json.dumps({"id": ident}),
                    status,
                    origin,
                ),
            )

    def listing(self, ident, **fields):
        return (
            "https://youtube.com/watch?v=" + ident,
            {"provider": "youtube", "collection": True},
            dict(id=ident, **fields),
        )

    def test_stops_lazy_paging_after_known_overlap(self):
        self.source()
        for key in ("a", "b", "c"):
            self.known(key)
        closed = []

        def entries(*args):
            try:
                for key in ("new", "a", "b", "c"):
                    yield self.listing(key)
                self.fail("fetched another old page")
            finally:
                closed.append(True)

        with (
            patch.object(s.providers, "iter_youtube_uploads", side_effect=entries),
            patch.object(s.providers, "extract") as extract,
        ):
            self.assertTrue(discovery.discover(s, self.url, self.opts))
        self.assertEqual(closed, [True])
        extract.assert_not_called()
        with s.db() as con:
            self.assertEqual(
                con.execute(
                    "SELECT origin,status FROM upload_inventory WHERE key='youtube:video:new'"
                ).fetchone()[:],
                ("new", "pending"),
            )

    def test_failed_listing_keeps_discovered_jobs_without_advancing_full_checkpoint(
        self,
    ):
        self.source(full=False)

        def entries(*args):
            yield self.listing("new")
            raise RuntimeError("network stopped")

        with patch.object(s.providers, "iter_youtube_uploads", side_effect=entries):
            with self.assertRaises(RuntimeError):
                discovery.discover(s, self.url, self.opts)
        with s.db() as con:
            self.assertEqual(
                con.execute("SELECT full_checked FROM upload_sources").fetchone()[0], 0
            )
            self.assertEqual(
                con.execute("SELECT status FROM upload_inventory").fetchone()[0],
                "pending",
            )

    def test_first_year_listing_stops_at_old_boundary_without_pruning(self):
        self.source(full=False)
        entries = [self.listing("current", upload_date="20260202")]
        entries += [
            self.listing("old" + str(i), upload_date="20240101") for i in range(4)
        ]

        def generate(*args):
            yield from entries[:4]
            self.fail("read past year boundary")

        with s.db() as con:
            con.execute(
                "INSERT INTO item_sources VALUES(?,?)", (self.url, "retained-existing")
            )
        with patch.object(s.providers, "iter_youtube_uploads", side_effect=generate):
            discovery.discover(s, self.url, self.opts)
        with s.db() as con:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM upload_inventory WHERE status='pending'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute("SELECT key FROM item_sources").fetchone()[0],
                "retained-existing",
            )

    def test_new_jobs_are_not_capped_by_historical_batch(self):
        self.source()
        for i in range(4):
            self.known(str(i), origin="new", status="pending")
        for i in range(4, 8):
            self.known(str(i), status="pending")
        with (
            patch.dict(s.CFG, {"backfill_batch_size": 1}),
            patch.object(s, "sync_source", return_value=False) as index,
        ):
            discovery.process(s, {self.url: self.opts})
        self.assertEqual(index.call_count, 5)
        with s.db() as con:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM upload_inventory WHERE retry_after>0"
                ).fetchone()[0],
                5,
            )

    def test_exact_old_date_does_not_probe_or_publish(self):
        video = info()
        video["upload_date"] = "20251231"
        entry = dict(video, _since="20260101")
        with (
            patch.object(s.providers, "extract", return_value=video),
            patch.object(s, "save_item") as save,
            patch.object(s.playback, "inspect_layout") as probe,
        ):
            self.assertTrue(
                s.sync_source(
                    self.url,
                    self.opts,
                    entries=[(video["webpage_url"], {"provider": "youtube"}, entry)],
                    prune=False,
                )
            )
        save.assert_not_called()
        probe.assert_not_called()

    def test_empty_reconciliation_does_not_discard_queued_work(self):
        self.source(full=False)
        self.known("queued", status="pending")
        with patch.object(s.providers, "iter_youtube_uploads", return_value=iter(())):
            self.assertFalse(discovery.discover(s, self.url, self.opts))
        with s.db() as con:
            self.assertEqual(
                con.execute("SELECT status FROM upload_inventory").fetchone()[0],
                "pending",
            )
            self.assertEqual(
                con.execute("SELECT full_checked FROM upload_sources").fetchone()[0], 0
            )

    def test_completed_year_window_discards_only_unseen_historical_jobs(self):
        self.source(full=False)
        self.known("ancient-queued", status="pending")
        self.known("existing-old", status="done")

        def entries(*args):
            yield self.listing("current", upload_date="20260202")

        with patch.object(s.providers, "iter_youtube_uploads", side_effect=entries):
            discovery.discover(s, self.url, self.opts)
        with s.db() as con:
            rows = dict(con.execute("SELECT key,status FROM upload_inventory"))
        self.assertEqual(rows["youtube:video:ancient-queued"], "ignored")
        self.assertEqual(rows["youtube:video:existing-old"], "done")
        self.assertEqual(rows["youtube:video:current"], "pending")

    def test_restricted_listing_does_not_consume_metadata_backfill_quota(self):
        self.source()
        self.known("members", status="pending")
        self.known("public", status="pending")
        with s.db() as con:
            con.execute(
                "UPDATE upload_inventory SET entry=? WHERE key='youtube:video:members'",
                (json.dumps({"id": "members", "availability": "subscriber_only"}),),
            )
        with (
            patch.dict(s.CFG, {"backfill_batch_size": 1}),
            patch.object(s, "sync_source", return_value=False) as index,
        ):
            discovery.process(s, {self.url: self.opts})
        self.assertEqual(index.call_count, 1)
        self.assertEqual(index.call_args.kwargs["entries"][0][2]["id"], "public")
        with s.db() as con:
            self.assertEqual(
                con.execute(
                    "SELECT status FROM upload_inventory WHERE key='youtube:video:members'"
                ).fetchone()[0],
                "restricted",
            )

    def test_changed_playback_layout_queues_only_that_item_and_wakes_scanner(self):
        video = info()
        self.source()
        self.known(video["id"], status="done")
        item = s.providers.normalize(video, {"collection": True})
        s.save_item(item, s.playback.choose(video), self.url)
        with (
            patch.object(s.providers, "extract", return_value=video),
            patch.object(
                s.playback, "choose_pinned", side_effect=ValueError("changed layout")
            ),
            patch.object(s.SCAN_WAKE, "set") as wake,
        ):
            with self.assertRaises(ValueError):
                s.resolve(item["key"], force=True)
        wake.assert_called_once()
        with s.db() as con:
            self.assertEqual(
                tuple(
                    con.execute("SELECT status,origin FROM upload_inventory").fetchone()
                ),
                ("pending", "new"),
            )

    def test_auth_restrictions_are_distinct_from_retryable_failures(self):
        self.assertEqual(
            discovery.restriction(RuntimeError("Sign in to confirm your age")),
            "needs_auth",
        )
        self.assertEqual(
            discovery.restriction(
                RuntimeError("Join this channel for members-only content")
            ),
            "subscriber_only",
        )
        self.assertIsNone(discovery.restriction(RuntimeError("network timeout")))

    def test_raw_ytdlp_entries_are_not_materialized(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)

        def entries():
            yield {
                "id": "first",
                "url": "https://youtube.com/watch?v=first",
                "ie_key": "Youtube",
            }
            self.fail("eagerly read the next page")

        client.extract_info.return_value = {"id": "channel", "entries": entries()}
        with patch.object(s.providers.yt_dlp, "YoutubeDL", return_value=client):
            iterator = s.providers.iter_youtube_uploads(self.url)
            self.assertEqual(next(iterator)[2]["id"], "first")
            iterator.close()
        self.assertFalse(client.extract_info.call_args.kwargs["process"])
