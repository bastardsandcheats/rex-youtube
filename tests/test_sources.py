"""Provider-neutral behavior, with no live websites required."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import playback
import providers
import service as s


def info(extractor="Youtube", ident="123"):
    return {
        "id": ident,
        "extractor_key": extractor,
        "webpage_url": "https://videos.example/watch/" + ident,
        "title": "Open-source electronics workshop",
        "description": "A public maker lesson.",
        "channel_id": "maker",
        "channel": "Maker Lab",
        "upload_date": "20260102",
        "duration": 120,
        "thumbnail": "https://images.example/thumb.jpg",
        "formats": [
            {
                "url": "https://cdn.example/movie.mp4?signature=transient-fixture",
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "mp4a.40.2",
                "width": 1920,
                "height": 1080,
                "asr": 48000,
            }
        ],
    }


class SourceTests(unittest.TestCase):
    def test_detection_uses_extractors_for_supported_sites(self):
        examples = {
            "youtube": "https://www.youtube.com/@TechnologyConnections",
            "vimeo": "https://vimeo.com/1084537",
            "dailymotion": "https://www.dailymotion.com/video/x7bs2wf",
            "twitch": "https://www.twitch.tv/codinggarden",
            "peertube": "https://framatube.org/videos/watch/9c9de5e8-0a1e-484a-b099-e80766180a6d",
            "bilibili": "https://www.bilibili.com/video/BV15z4y1F7tK/",
            "tiktok": "https://www.tiktok.com/@astrosamantha/video/7166258449645407493",
        }
        for expected, url in examples.items():
            with self.subTest(expected):
                self.assertEqual(providers.detect(url), expected)

    def test_each_site_has_the_same_normalized_shape(self):
        expected = set(providers.normalize(info()))
        for extractor in (
            "Vimeo",
            "Dailymotion",
            "TwitchVod",
            "PeerTube",
            "BiliBili",
            "TikTok",
        ):
            item = providers.normalize(
                info(extractor), {"collection": True, "name": "Maker Lab"}
            )
            self.assertEqual(set(item), expected)
            self.assertEqual(item["published"], "2026-01-02")
            self.assertEqual(item["kind"], "episode")
            self.assertNotIn("signature=", json.dumps(item))

    def test_provider_identifiers_never_collide(self):
        self.assertNotEqual(
            providers.normalize(info("Youtube"))["key"],
            providers.normalize(info("Vimeo"))["key"],
        )

    def test_standalone_videos_are_movies_and_collections_are_shows(self):
        self.assertEqual(providers.normalize(info())["kind"], "movie")
        self.assertEqual(
            providers.normalize(info(), {"collection": True})["kind"], "episode"
        )

    def test_public_embed_without_upload_date_remains_usable(self):
        video = info("Vimeo")
        video.pop("upload_date")
        self.assertEqual(providers.normalize(video)["published"], "")

    def test_collection_enumeration_uses_extractor_webpage_references(self):
        collection = {
            "id": "maker",
            "extractor_key": "VimeoUser",
            "title": "Maker Lab",
            "entries": [
                {"id": "1", "url": "https://vimeo.com/1"},
                {"id": "2", "url": "https://vimeo.com/2"},
            ],
        }
        with (
            patch.object(providers, "extract", return_value=collection),
            patch.object(providers, "creator_metadata", return_value={}),
        ):
            entries = list(providers.enumerate_source("https://vimeo.com/maker"))
        self.assertEqual(
            {r[0] for r in entries}, {"https://vimeo.com/1", "https://vimeo.com/2"}
        )
        self.assertTrue(all(r[1]["collection"] for r in entries))

    def test_live_identity_is_stable_when_broadcast_changes(self):
        a = info("TwitchStream", "broadcast-one")
        a["is_live"] = True
        b = {**a, "id": "broadcast-two"}
        one, two = providers.normalize(a), providers.normalize(b)
        self.assertEqual(one["key"], two["key"])
        self.assertEqual(one["kind"], "movie")

    def test_hls_segment_urls_containing_m3u8_are_still_binary_segments(self):
        calls = []

        def relay(url, headers, ext):
            calls.append(ext)
            return "https://relay.example/segment." + ext

        result = playback.rewrite_hls(
            "#EXTM3U\nfile/index.m3u8/segment/1.ts\n",
            "https://cdn.example/list",
            {},
            relay,
        )
        self.assertEqual(calls, ["ts"])
        self.assertIn("segment.ts", result)

    def test_progressive_and_separate_tracks_choose_different_paths(self):
        combined = info()
        self.assertEqual(playback.choose(combined)["kind"], "http")
        separate = info()
        separate["formats"][0]["acodec"] = "none"
        separate["formats"].append(
            {
                "url": "https://cdn.example/audio.mp4",
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
            }
        )
        plan = playback.choose(separate)
        self.assertEqual(plan["kind"], "dash")
        manifest = playback.dash_manifest(
            plan, lambda url, h, ext: "https://relay.example/" + url.rsplit("/", 1)[-1]
        )
        self.assertIn("audio/mp4", manifest)
        self.assertIn("video/mp4", manifest)
        self.assertEqual(manifest.count("<Representation "), 2)

    def test_live_ts_metadata_has_no_fabricated_duration(self):
        video = info("TwitchStream")
        video.update(is_live=True, duration=None)
        plan = playback.choose(video)
        probe = playback.media_info(plan)
        self.assertEqual(plan["kind"], "live")
        self.assertNotIn("duration", probe["format"])
        self.assertEqual(probe["format"]["format_name"], "mpegts")

    def test_network_failure_does_not_remove_live_movies(self):
        with (
            patch.object(s, "setup_rex"),
            patch.object(s, "flush_ready"),
            patch.object(
                providers,
                "source_lines",
                return_value=[("https://www.twitch.tv/codinggarden", "")],
            ),
            patch.object(s, "sync_source", side_effect=requests.Timeout),
            patch.object(s, "remove_offline") as remove,
            self.assertLogs(s.LOG, level="WARNING"),
        ):
            s.sync()
        remove.assert_not_called()

    def test_offline_confirmation_removes_only_that_sources_live_movie(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            s.init_db()
            with s.db() as con:
                for name in ("one", "two"):
                    con.execute(
                        "INSERT INTO items VALUES(?,?,?,?,?,?)",
                        (
                            name,
                            json.dumps({"is_live": True, "source_entry": name}),
                            "live:" + name,
                            name + ".ts",
                            None,
                            "published",
                        ),
                    )
            with patch.object(s, "rex") as rex:
                s.remove_offline("one")
            self.assertEqual(rex.call_args.args[2], {"ids": ["live:one"]})
            with s.db() as con:
                self.assertEqual(
                    [r[0] for r in con.execute("SELECT key FROM items")], ["two"]
                )

    def test_signed_urls_and_anonymous_cookies_are_not_persisted(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            s.init_db()
            video = info()
            plan = playback.choose(video)
            plan["headers"] = {"Cookie": "transient-cookie"}
            s.save_item(providers.normalize(video), plan, video["webpage_url"])
            with s.db() as con:
                text = con.execute("SELECT metadata FROM items").fetchone()[0]
            self.assertNotIn("signature=", text)
            self.assertNotIn("transient-cookie", text)

    def test_recording_vod_is_persistent_rather_than_a_live_movie(self):
        video = info("TwitchVod")
        video["is_live"] = True
        self.assertFalse(providers.normalize(video)["is_live"])
        self.assertNotEqual(playback.choose(video)["kind"], "live")

    def test_malformed_source_does_not_hide_following_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.txt"
            path.write_text("not-a-url\nhttps://vimeo.com/1084537\n")
            with self.assertLogs("providers", level="WARNING"):
                self.assertEqual(
                    list(providers.source_lines(path)),
                    [("https://vimeo.com/1084537", "")],
                )

    def test_dash_manifest_advertises_profile_for_ffmpeg_autodetection(self):
        video = info()
        video["formats"][0].update(
            protocol="http_dash_segments",
            fragments=[
                {"path": "init.mp4"},
                {"path": "1.m4s", "duration": 4},
            ],
            fragment_base_url="https://cdn.example/",
        )
        text = playback.dash_manifest(playback.choose(video), lambda u, h, e: u)
        self.assertIn("urn:mpeg:dash:profile:isoff-main:2011", text)
        self.assertIn('sourceURL="https://cdn.example/init.mp4"', text)

    def test_legacy_migration_keeps_feed_identity_and_rex_key(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            with s.db() as con:
                con.executescript(
                    """CREATE TABLE channels(id TEXT,name TEXT,description TEXT,art TEXT);
                CREATE TABLE videos(id TEXT,channel_id TEXT,title TEXT,description TEXT,published TEXT,
                duration REAL,episode INTEGER,info TEXT,rating_key TEXT);"""
                )
                con.execute(
                    "INSERT INTO channels VALUES(?,?,?,?)",
                    ("maker", "Maker Lab", "", ""),
                )
                probe = {"media_info": {"streams": [], "format": {"duration": "120"}}}
                con.execute(
                    "INSERT INTO videos VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "abc",
                        "maker",
                        "Circuit lab",
                        "",
                        "2026-01-02",
                        120,
                        3,
                        json.dumps(probe),
                        "789",
                    ),
                )
            s.init_db()
            s.init_db()
            with s.db() as con:
                rows = list(con.execute("SELECT * FROM items"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["feed_id"], "yt-hls2-abc")
            self.assertEqual(rows[0]["rating_key"], "789")
            self.assertEqual(json.loads(rows[0]["metadata"])["episode"], 3)

    def test_muxed_hls_uses_actual_header_indices(self):
        from types import SimpleNamespace

        video = info("Dailymotion")
        video["formats"][0]["protocol"] = "m3u8_native"
        plan = playback.choose(video)
        actual = [
            {"index": 0, "codec_type": "audio", "codec_name": "aac"},
            {"index": 1, "codec_type": "video", "codec_name": "h264"},
        ]
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"streams": actual}))
        with patch.object(playback.subprocess, "run", return_value=result) as probe:
            playback.inspect_layout(plan)
        self.assertIn("%+#1", probe.call_args.args[0])
        self.assertEqual(playback.media_info(plan)["streams"], actual)

    def test_progressive_layout_does_not_require_a_probe(self):
        with patch.object(playback.subprocess, "run") as probe:
            playback.inspect_layout(playback.choose(info()))
        probe.assert_not_called()

    def test_all_live_backends_share_one_movie_library(self):
        for extractor in ("TwitchStream", "TikTokLive", "Example:Streamlink"):
            video = info(extractor)
            video["is_live"] = True
            item = providers.normalize(video, {"collection": True})
            self.assertEqual(item["kind"], "movie")
            self.assertEqual(s.library_name(item), "Live Streams")

    def test_collection_uses_creator_instead_of_generated_playlist_title(self):
        video = info("TwitchVod")
        video["channel"] = "Maker Lab"
        item = providers.normalize(
            video, {"collection": True, "name": "Maker Lab - All Videos sorted by Date"}
        )
        self.assertEqual(item["creator_name"], "Maker Lab")
        item = providers.normalize(video, {"collection": True, "alias": "My Workshops"})
        self.assertEqual(item["creator_name"], "My Workshops")

    def test_streamlink_can_resolve_a_non_twitch_live_site(self):
        from unittest.mock import MagicMock
        import streamlink

        live = info("Example:Streamlink")
        live["is_live"] = True
        with (
            patch.object(providers, "detect", return_value="generic"),
            patch.object(streamlink, "Streamlink", return_value=MagicMock()),
            patch.object(providers, "streamlink_live", return_value=live) as resolve,
        ):
            extracted = providers.extract("https://live.example/broadcast")
        resolve.assert_called_once_with("https://live.example/broadcast")
        self.assertEqual(s.library_name(providers.normalize(extracted)), "Live Streams")

    def test_vimeo_public_oembed_restores_upload_year(self):
        from unittest.mock import MagicMock

        video = info("Vimeo", "54502942")
        video.pop("upload_date")
        client = MagicMock()
        client.extract_info.return_value = video
        response = MagicMock()
        response.json.return_value = {
            "upload_date": "2012-11-28 23:09:00",
            "author_url": "https://vimeo.com/businessofsoftware",
        }
        with (
            patch.object(providers, "detect", return_value="vimeo"),
            patch.object(providers.yt_dlp, "YoutubeDL") as factory,
            patch.object(providers.requests, "get", return_value=response),
        ):
            factory.return_value.__enter__.return_value = client
            extracted = providers.extract("https://vimeo.com/54502942")
        self.assertEqual(extracted["upload_date"], "20121128")
        self.assertEqual(extracted["uploader_id"], "businessofsoftware")

    def test_twitch_channel_defaults_to_archived_vods(self):
        collection = {"id": "maker", "extractor_key": "TwitchVideos", "entries": []}
        with patch.object(providers, "extract", return_value=collection) as extract:
            list(providers.enumerate_source("https://www.twitch.tv/csharpfritz/videos"))
        self.assertEqual(
            extract.call_args.args[0],
            "https://www.twitch.tv/csharpfritz/videos?filter=archives&sort=time",
        )

    def test_publication_sets_year_and_keeps_empty_source_fields_from_erasing_metadata(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            s.init_db()
            video = info()
            video["description"] = ""
            item = providers.normalize(video, {"collection": True})
            s.save_item(item, playback.choose(video), video["webpage_url"])
            with s.db() as con:
                row = dict(con.execute("SELECT * FROM items").fetchone())
            replies = [
                {},
                {"items": [{"id": row["feed_id"], "ratingKey": "55"}]},
                {
                    "MediaContainer": {
                        "Metadata": [
                            {"grandparentRatingKey": "10", "parentRatingKey": "11"}
                        ]
                    }
                },
            ]
            with (
                patch.object(s, "rex", side_effect=replies),
                patch.object(s, "ensure_library", return_value="1"),
                patch.object(s, "edit") as edit,
                patch.object(s, "upload_art"),
            ):
                s.publish(row)
            fields = edit.call_args_list[0].args[2]
            self.assertEqual(fields["year"], 2026)
            self.assertEqual(fields["studio"], "YouTube")
            self.assertNotIn("summary", fields)
            self.assertNotIn("summary", edit.call_args_list[1].args[2])
