"""Settings inheritance, validation and configured-source checks."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from app import config, source, service as s
from app.provider_registry import get_definition
from fixtures import info


def fixture_url(provider, kind):
    return next(
        t["url"] for t in get_definition(provider).TEST_URLS if t["kind"] == kind
    )


def document():
    return {
        "rex": {"url": "http://rex.invalid"},
        "defaults": {"max_items": 40},
        "sites": {
            "youtube": {
                "library": "Learning",
                "max_items": 20,
                "sources": [
                    {
                        "url": fixture_url("youtube", "collection"),
                        "type": "show",
                        "name": "My workshop",
                        "year_seasons": False,
                        "season": 2,
                        "max_items": 7,
                    }
                ],
            }
        },
    }


class ConfigTests(unittest.TestCase):
    def test_source_overrides_site_overrides_defaults(self):
        cfg = config.normalize(document())
        entry = cfg["source_entries"][0]
        settings = entry["options"]
        self.assertEqual(settings["max_items"], 7)
        self.assertEqual(settings["library"], "Learning")
        self.assertFalse(settings["year_seasons"])
        self.assertEqual(settings["season"], 2)
        self.assertEqual(settings["name"], "My workshop")

    def test_comments_are_accepted_and_never_rewritten_on_load(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root, "config/config.yml")
            p.parent.mkdir()
            text = "# My comments stay here\n" + yaml.safe_dump(document())
            p.write_text(text)
            self.assertEqual(
                config.load(root)["source_entries"][0]["options"]["max_items"], 7
            )
            self.assertEqual(p.read_text(), text)

    def test_disabled_sites_and_entries_do_not_enter_scan_list(self):
        data = document()
        data["sites"]["youtube"]["sources"][0]["enabled"] = False
        self.assertEqual(config.normalize(data)["source_entries"], [])
        data["sites"]["youtube"]["sources"][0]["enabled"] = True
        data["sites"]["youtube"]["enabled"] = False
        self.assertEqual(config.normalize(data)["source_entries"], [])

    def test_invalid_values_and_misspellings_fail_before_scan(self):
        for field, value in [
            ("max_items", 0),
            ("max_items", True),
            ("year_seasons", "false"),
            ("type", "series"),
            ("max_height", 0),
            ("year_season", True),
        ]:
            data = document()
            data["sites"]["youtube"]["sources"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                config.normalize(data)

    def test_duplicate_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root, "config.yml")
            p.write_text("sites:\n  youtube: {}\n  youtube: {}\n")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                config.load_yaml(p)

    def test_live_entry_enables_check_without_site_default(self):
        data = document()
        data["sites"]["youtube"]["sources"][0].update(
            url=fixture_url("youtube", "live"), type="live"
        )
        self.assertTrue(
            config.normalize(data)["source_entries"][0]["options"]["check_live"]
        )

    def test_live_url_discovery_is_opt_in_and_owned_by_each_site(self):
        for site in ("youtube", "twitch", "tiktok"):
            url = fixture_url(site, "collection")
            without = source.source_targets(url, False)
            with_live = source.source_targets(url, True)
            self.assertEqual(len(without), 1)
            self.assertEqual(len(with_live), 2)
            self.assertEqual(with_live[0], without[0])
        item = fixture_url("youtube", "video")
        self.assertEqual(source.source_targets(item, True), [item])

    def test_configured_live_targets_keep_live_type_separate_from_channel_show(self):
        entry = {
            "url": fixture_url("youtube", "collection"),
            "options": {"type": "show", "check_live": True},
        }
        targets = list(source.configured_targets(entry))
        self.assertEqual(targets[0]["options"]["type"], "show")
        self.assertEqual(targets[1]["options"]["type"], "live")
        self.assertEqual(entry["options"]["type"], "show")

    def test_show_overrides_reversible_and_ids_stable(self):
        item = source.normalize(info(), {"collection": True})
        original_key = item["key"]
        settings = config.normalize(document())["source_entries"][0]["options"]
        changed = source.apply_settings(item, settings, {"collection": True})
        self.assertEqual(changed["season"], 2)
        self.assertEqual(changed["creator_name"], "My workshop")
        self.assertEqual(s.library_name(changed), "Learning")
        restored = source.apply_settings(changed, {})
        self.assertEqual(restored["creator_name"], item["creator_name"])
        self.assertEqual(restored["season"], 2026)
        self.assertEqual(restored["key"], original_key)

    def test_movie_and_live_names_use_their_separate_library_overrides(self):
        item = source.normalize(info())
        renamed = source.apply_settings(
            item, {"type": "movie", "name": "Workshop film", "movie_library": "Lessons"}
        )
        self.assertEqual(renamed["title"], "Workshop film")
        self.assertEqual(s.library_name(renamed), "Lessons")
        item.update(is_live=True)
        live = source.apply_settings(
            item, {"name": "My stream", "live_library": "Live Lessons"}
        )
        self.assertEqual(live["title"], "My stream")
        self.assertEqual(s.library_name(live), "Live Lessons")

    def test_configured_checker_reads_yaml_entries_and_inherited_options(self):
        from scripts import check_providers

        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "settings.yml")
            path.write_text(yaml.safe_dump(document()))
            fake = {"result": "passed", "checks": {}}
            with (
                patch(
                    "sys.argv",
                    [
                        "check_providers.py",
                        "--configured",
                        "--config-file",
                        str(path),
                        "--report-dir",
                        str(Path(root, "reports")),
                    ],
                ),
                patch.object(
                    check_providers.checks, "check_url", return_value=fake
                ) as check,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(check_providers.main(), 0)
            self.assertEqual(check.call_count, 1)
            self.assertEqual(check.call_args.kwargs["settings"]["max_items"], 7)
            self.assertFalse(check.call_args.kwargs["settings"]["year_seasons"])

    def test_fixed_season_reaches_the_feed_payload(self):
        from app import playback

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            s.init_db()
            video = info()
            item = source.apply_settings(
                source.normalize(video, {"collection": True}),
                {"year_seasons": False, "season": 2},
            )
            s.save_item(item, playback.choose(video), video["webpage_url"])
            with s.db() as con:
                row = dict(con.execute("SELECT * FROM items").fetchone())
            replies = [
                {},
                {"items": [{"id": row["feed_id"], "ratingKey": "55"}]},
                {"MediaContainer": {"Metadata": [{}]}},
            ]
            with (
                patch.object(s, "rex", side_effect=replies) as rex,
                patch.object(s, "ensure_library", return_value="1"),
                patch.object(s, "edit"),
                patch.object(s, "upload_art"),
            ):
                s.publish(row)
            payload = rex.call_args_list[0].args[2]["items"][0]
            self.assertEqual(payload["season"], 2)
            self.assertEqual(payload["kind"], "episode")

    def test_public_yaml_example_loads_with_all_comments_and_options(self):
        cfg = config.load_yaml(
            Path(__file__).resolve().parents[1] / "config/config.example.yml"
        )
        self.assertTrue(cfg["source_entries"])
        self.assertEqual(cfg["log_level"], "INFO")
