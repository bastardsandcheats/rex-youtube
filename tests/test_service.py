import unittest
import json
from unittest.mock import patch
from app import service as s, playback, source


class Tests(unittest.TestCase):
    def formats(self):
        return {
            "duration": 120,
            "formats": [
                {
                    "protocol": "m3u8_native",
                    "format_id": "video",
                    "height": 720,
                    "width": 1280,
                    "fps": 24,
                    "vcodec": "avc1.4d401f",
                    "acodec": "none",
                    "url": "https://example.googlevideo.com/video",
                },
                {
                    "protocol": "m3u8_native",
                    "format_id": "audio",
                    "vcodec": "none",
                    "format_note": "English original",
                    "audio_channels": 2,
                    "asr": 44100,
                    "language": "en",
                    "url": "https://example.googlevideo.com/audio",
                },
            ],
        }

    def test_selected_formats_create_truthful_remux_metadata(self):
        info = self.formats()
        m = playback.media_info(playback.choose(info))
        self.assertEqual(m["format"]["format_name"], "hls")
        self.assertEqual(m["streams"][0]["index"], 1)
        self.assertEqual(m["streams"][1]["index"], 0)
        self.assertEqual(m["streams"][0]["codec_name"], "h264")
        self.assertEqual(m["streams"][0]["width"], 1280)
        self.assertEqual(m["streams"][1]["sample_rate"], "44100")
        self.assertEqual(m["format"]["duration"], "120")

    def test_no_supported_formats_is_not_published_as_playable(self):
        with self.assertRaises(ValueError):
            playback.choose({"formats": []})

    def test_playlist_rewrite_uses_local_bounded_memory_tokens(self):
        with patch.object(s, "local_url", return_value="http://relay:8790"):
            text = playback.rewrite_hls(
                '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\nsegment.ts\n',
                "https://media.googlevideo.com/list/index.m3u8",
                {},
                lambda url, headers, ext: s.token_url("abcdefghijk", url, headers, ext),
            )
        self.assertNotIn("googlevideo.com", text)
        self.assertEqual(text.count("http://relay:8790/hls/abcdefghijk/"), 2)

    def test_relay_is_not_an_open_proxy(self):
        with self.assertRaises(ValueError):
            s.token_url("abcdefghijk", "http://127.0.0.1/private", {})

    def test_channel_badge_uses_avatar_instead_of_banner(self):
        listing = {
            "thumbnails": [
                {"url": "banner", "width": 2560, "height": 424},
                {"url": "avatar", "width": 900, "height": 900},
            ]
        }
        self.assertEqual(source.channel_art(listing), "avatar")

    def test_metadata_edit_locks_title_and_summary(self):
        with patch.object(s, "rex") as rex:
            s.edit("123", 4, {"title": "YouTube title", "summary": "Description"})
        params = rex.call_args.kwargs["params"]
        self.assertEqual(params["title.value"], "YouTube title")
        self.assertEqual(params["summary.locked"], 1)

    def test_upload_years_have_separate_chronological_episode_numbers(self):
        import tempfile
        from pathlib import Path

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(s, "ROOT", Path(directory)),
        ):
            s.init_db()
            with s.db() as con:
                for ident, date, number in [
                    ("new", "2026-10-21", 1),
                    ("older", "2026-02-01", 9),
                    ("lastyear", "2025-12-31", 8),
                ]:
                    con.execute(
                        "INSERT INTO items VALUES(?,?,?,?,?,?)",
                        (
                            ident,
                            json.dumps(
                                {
                                    "key": ident,
                                    "provider": "youtube",
                                    "creator_id": "channel",
                                    "kind": "episode",
                                    "published": date,
                                    "episode": number,
                                }
                            ),
                            ident,
                            ident + ".mp4",
                            None,
                            "published",
                        ),
                    )
            s.reorder("channel")
            with s.db() as con:
                rows = {
                    r["key"]: (json.loads(r["metadata"])["episode"], r["status"])
                    for r in con.execute("SELECT * FROM items")
                }
            self.assertEqual(
                rows,
                {"older": (1, "ready"), "new": (2, "ready"), "lastyear": (1, "ready")},
            )

    def test_webp_thumbnail_is_uploaded_as_real_jpeg(self):
        import io
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (32, 18), "red").save(raw, "WEBP")
        converted = s.jpeg_art(raw.getvalue())
        self.assertTrue(converted.startswith(b"\xff\xd8"))
        with Image.open(io.BytesIO(converted)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (32, 18))

    def test_rex_requests_support_standard_token_authentication(self):
        with (
            patch.dict(s.os.environ, {"REX_TOKEN": "example-access-token"}),
            patch.object(s.requests, "request") as request,
        ):
            request.return_value.content = b""
            s.rex("GET", "/library/sections")
            self.assertEqual(
                request.call_args.kwargs["headers"]["X-Plex-Token"],
                "example-access-token",
            )
        with patch.dict(s.os.environ, {"REX_TOKEN": "example-access-token"}):
            self.assertEqual(
                s.rex_headers({"Content-Type": "image/jpeg"})["X-Plex-Token"],
                "example-access-token",
            )


if __name__ == "__main__":
    unittest.main()
