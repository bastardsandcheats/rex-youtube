"""Focused source-display, stable-placement, probing and reconciliation regressions."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from app import config, playback, service as s, source
from app import collections as groups
from fixtures import info


class FeedFeatures(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root_patch = patch.object(s, "ROOT", Path(temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.cfg_patch = patch.dict(s.CFG, config.normalize({}), clear=True)
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        s.init_db()
        self.video = info()
        self.item = source.apply_settings(
            source.normalize(self.video, {"collection": True}), {"type": "show"}
        )

    def save(self, video=None, options=None):
        video = video or self.video
        item = source.apply_settings(
            source.normalize(video, {"collection": True}), options or {"type": "show"}
        )
        s.save_item(item, playback.choose(video), "https://videos.example/channel")
        return next(
            row
            for row in s.catalog_rows()
            if json.loads(row["metadata"])["source_id"] == video["id"]
        )

    def test_display_has_source_fields_without_library_edits_and_exact_file_size(self):
        row = self.save(options={"type": "movie"})
        with patch.object(s, "ensure_library", return_value="1"):
            payload = s.feed_item(row)
        self.assertEqual(payload["size"], 123456)
        self.assertEqual(payload["display"]["title"], self.item["title"])
        self.assertEqual(payload["display"]["date"], "2026-01-02")
        self.assertNotIn("locked", json.dumps(payload))

    def test_new_backfill_gets_next_number_without_moving_existing_episodes(self):
        original = self.save()
        older = copy.deepcopy(self.video)
        older.update(id="older", upload_date="20250101")
        # Same fixed season makes the backfill join the existing placement group.
        original = self.save(
            options={"type": "show", "year_seasons": False, "season": 1}
        )
        added = self.save(older, {"type": "show", "year_seasons": False, "season": 1})
        self.assertEqual(json.loads(original["metadata"])["episode"], 1)
        self.assertEqual(json.loads(added["metadata"])["episode"], 2)
        self.assertEqual(json.loads(s.catalog_rows()[0]["metadata"])["episode"], 1)

    def test_format_change_versions_delivery_and_refresh_cannot_change_formats(self):
        row = self.save()
        changed = copy.deepcopy(self.video)
        changed["formats"][0]["height"] = 720
        with self.assertRaises(ValueError):
            playback.choose_pinned(
                changed, 1080, json.loads(row["metadata"])["selection"]
            )
        new = self.save(changed)
        self.assertNotEqual(row["feed_id"], new["feed_id"])
        self.assertNotEqual(row["relay_path"], new["relay_path"])
        self.assertEqual(
            json.loads(row["metadata"])["episode"],
            json.loads(new["metadata"])["episode"],
        )
        with s.db() as con:
            self.assertEqual(
                con.execute("SELECT id FROM retired_versions").fetchone()[0],
                row["feed_id"],
            )

    def test_full_probe_stores_whole_response_and_disabling_does_not_run_it(self):
        whole = {
            "format": {"format_name": "mov", "size": "777"},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "chapters": [{"id": 1}],
        }
        with (
            patch.object(playback, "full_probe", return_value=whole) as probe,
            patch.object(s, "local_url", return_value="http://relay"),
        ):
            row = self.save(options={"type": "show", "probe": True})
            self.assertEqual(json.loads(row["metadata"])["media_info"], whole)
            self.assertEqual(json.loads(row["metadata"])["size"], 777)
            probe.assert_called_once_with("http://relay/stream/" + row["relay_path"])
        with patch.object(playback, "full_probe") as probe:
            self.save(options={"type": "show", "probe": False})
            probe.assert_not_called()

    def test_probe_false_reaches_scanner_without_even_header_probing(self):
        opts = dict(config.DEFAULT_OPTIONS, probe=False)
        with (
            patch.object(
                source,
                "enumerate_source",
                return_value=[
                    (
                        self.video["webpage_url"],
                        {"collection": True, "provider": "youtube"},
                        self.video,
                    )
                ],
            ),
            patch.object(source, "extract", return_value=self.video),
            patch.object(playback, "inspect_layout") as header,
        ):
            self.assertTrue(s.sync_source("https://videos.example/channel", opts))
        header.assert_not_called()

    def test_old_yaml_loads_and_collection_is_independent_of_show_type(self):
        data = {
            "sites": {
                "youtube": {"sources": [{"url": "https://videos.example/channel"}]}
            }
        }
        old = config.normalize(data)["source_entries"][0]["options"]
        self.assertEqual(old["type"], "auto")
        self.assertEqual(old["probe"], "headers")
        data["sites"]["youtube"].update(probe=False, collection="Level1Techs")
        options = config.normalize(data)["source_entries"][0]["options"]
        item = source.apply_settings(self.item, options)
        self.assertEqual(item["kind"], "episode")
        self.assertEqual(item["collection"], "Level1Techs")
        options.update(type="collection", collection=False)
        item = source.apply_settings(self.item, options)
        self.assertEqual(item["kind"], "movie")
        self.assertEqual(item["collection"], "Maker Lab")
        live = source.apply_settings({**self.item, "is_live": True}, options)
        self.assertEqual(live["collection"], "YouTube")

    def test_snapshot_session_restarts_but_shrink_guard_is_not_forced(self):
        error_response = Mock(status_code=409, text="no such snapshot session")
        error = requests.HTTPError(response=error_response)
        with patch.object(s, "rex", side_effect=[error, {}]) as request:
            self.assertTrue(s.snapshot())
            bodies = [call.args[2] for call in request.call_args_list]
            self.assertNotEqual(bodies[0]["session"], bodies[1]["session"])
            self.assertEqual([body["part"] for body in bodies], [0, 0])
        error_response.text = "would remove more than half"
        with (
            patch.object(s, "rex", side_effect=error) as request,
            self.assertRaises(requests.HTTPError),
        ):
            s.snapshot()
        request.assert_called_once()

    def test_failed_source_prevents_snapshot_or_pruning(self):
        entry = {"url": "https://videos.example/channel", "options": {}}
        with (
            patch.object(s, "setup_rex"),
            patch.object(s, "flush_ready", return_value=True),
            patch.object(config, "source_entries", return_value=[entry]),
            patch.object(source, "configured_targets", return_value=[entry]),
            patch.object(s, "sync_source", return_value=False),
            patch.object(s, "reconcile_sources") as prune,
            patch.object(s, "snapshot") as snapshot,
        ):
            s.sync()
        prune.assert_not_called()
        snapshot.assert_not_called()

    def test_cached_refresh_updates_metadata_but_retains_missing_optional_description(
        self,
    ):
        first = self.save()
        revised = copy.deepcopy(self.video)
        revised.update(title="Corrected title", description="")
        second = self.save(revised)
        item = json.loads(second["metadata"])
        self.assertEqual(item["title"], "Corrected title")
        self.assertEqual(item["description"], self.video["description"])
        self.assertEqual(first["feed_id"], second["feed_id"])

    def test_collection_guid_mismatch_stops_before_modification(self):
        row = self.save(options={"type": "movie", "collection": "Lessons"})
        row.update(status="published", rating_key="3")
        identity = json.dumps(["1", 1, "Lessons"])
        with s.db() as con:
            con.execute(groups.SCHEMA)
            con.execute(
                "INSERT INTO managed_collections VALUES(?,?,?)",
                (identity, "20", "ours"),
            )

        def request(method, path, **kw):
            if path == "/identity":
                return {"MediaContainer": {"machineIdentifier": "machine"}}
            if path == "/library/metadata/3":
                return {
                    "MediaContainer": {
                        "Metadata": [{"ratingKey": "3", "librarySectionID": 1}]
                    }
                }
            if path == "/library/collections/20":
                return {"MediaContainer": {"Directory": [{"guid": "somebody-elses"}]}}
            self.fail("Modified a collection after ownership changed: " + path)

        with self.assertRaisesRegex(ValueError, "GUID"):
            groups.reconcile([row], request, s.db)

    def test_full_probe_correction_versions_an_existing_inferred_layout(self):
        old = self.save()
        measured = {
            "format": {"format_name": "mov"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1920,
                    "height": 1080,
                }
            ],
        }
        with (
            patch.object(playback, "full_probe", return_value=measured),
            patch.object(s, "local_url", return_value="http://relay"),
        ):
            new = self.save(options={"type": "show", "probe": True})
        self.assertNotEqual(old["feed_id"], new["feed_id"])
        self.assertNotEqual(old["relay_path"], new["relay_path"])

    def test_source_lock_migration_keeps_a_different_user_title(self):
        legacy = {**self.item, "title": "Old source title"}
        row = {"rating_key": "5"}
        with patch.object(
            s,
            "rex",
            return_value={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "5",
                            "title": "User title",
                            "summary": legacy["description"],
                        }
                    ]
                }
            },
        ) as request:
            s.release_source_locks(row, legacy)
        params = request.call_args.kwargs["params"]
        self.assertNotIn("title.locked", params)
        self.assertEqual(params["summary.locked"], 0)

    def test_source_removal_preserves_a_shared_item_until_its_last_owner_leaves(self):
        row = self.save()
        with s.db() as con:
            con.execute(
                "INSERT INTO item_sources VALUES(?,?)",
                ("https://videos.example/second", row["key"]),
            )
        s.reconcile_sources({"https://videos.example/second"})
        self.assertEqual(len(s.catalog_rows()), 1)
        s.reconcile_sources(set())
        self.assertEqual(s.catalog_rows(), [])
        with s.db() as con:
            self.assertEqual(
                con.execute("SELECT id FROM retired_versions").fetchone()[0],
                row["feed_id"],
            )

    def test_capped_discovery_does_not_prune_old_cached_uploads(self):
        old = self.save()
        video = copy.deepcopy(self.video)
        video["id"] = "new-window"
        options = dict(config.DEFAULT_OPTIONS, max_items=1, probe=False)
        with (
            patch.object(
                source,
                "enumerate_source",
                return_value=[
                    (
                        video["webpage_url"],
                        {"collection": True, "provider": "youtube"},
                        video,
                    )
                ],
            ),
            patch.object(source, "extract", return_value=video),
        ):
            self.assertTrue(s.sync_source("https://videos.example/channel", options))
        s.reconcile_sources({"https://videos.example/channel"})
        self.assertIn(old["key"], {row["key"] for row in s.catalog_rows()})
