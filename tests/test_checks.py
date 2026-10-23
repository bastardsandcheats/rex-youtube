"""Check each success level, actual relay decoding, isolation and bounded failures."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from app import checks, playback, reporting, service as s, source
from fixtures import info


class CheckTests(unittest.TestCase):
    def test_playable_but_missing_metadata_is_partial(self):
        report = checks.new_report("https://video.example/watch?v=1&token=hidden")
        report["checks"] = {
            "extraction": {"status": "passed", "summary": "ok"},
            "metadata": {"status": "partial", "summary": "Missing poster"},
            "decode": {"status": "passed", "summary": "ok"},
            "seek": {"status": "failed", "summary": "no"},
        }
        result = checks.finish(report)
        self.assertEqual(result["result"], "partial")
        self.assertTrue(result["capabilities"]["playable"])
        self.assertFalse(result["capabilities"]["seekable"])
        self.assertFalse(result["capabilities"]["metadata_complete"])
        self.assertNotIn("hidden", result["url"])

    def test_failure_before_extraction_is_not_playable(self):
        with patch.object(
            source,
            "enumerate_source",
            side_effect=RuntimeError("https://cdn.example/?secret=hidden"),
        ):
            result = checks.evaluate("https://video.example/123")
        self.assertEqual(result["result"], "failed")
        self.assertFalse(result["capabilities"]["playable"])
        self.assertNotIn("hidden", json.dumps(result))

    def test_offline_is_distinguished_from_general_failure(self):
        with patch.object(
            source, "enumerate_source", side_effect=source.Unavailable("offline")
        ):
            result = checks.evaluate("https://video.example/live")
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(result["checks"]["decode"]["status"], "skipped")

    def test_missing_required_metadata_does_not_claim_playback(self):
        video = info()
        video.pop("id")
        with (
            patch.object(
                source,
                "enumerate_source",
                return_value=[("https://video.example/123", {}, {})],
            ),
            patch.object(source, "extract", return_value=video),
            patch.object(checks, "decode") as decode,
        ):
            result = checks.evaluate("https://video.example/123")
        self.assertEqual(result["checks"]["metadata"]["status"], "failed")
        self.assertEqual(result["checks"]["metadata"]["missing"], ["id"])
        decode.assert_not_called()

    def test_deadline_kills_worker_group_and_retains_partial_results(self):
        child = MagicMock(pid=12345, returncode=-9)
        progress = json.dumps(
            {
                "url": "https://video.example/123",
                "checks": {"extraction": {"status": "passed", "summary": "ok"}},
            }
        ).encode()
        child.communicate.side_effect = [
            subprocess.TimeoutExpired("worker", 10),
            (progress, None),
        ]
        with (
            patch.object(checks.subprocess, "Popen", return_value=child),
            patch.object(checks.os, "killpg") as kill,
        ):
            result = checks.check_url("https://video.example/123", timeout=10)
        kill.assert_called_once()
        self.assertEqual(result["result"], "partial")
        self.assertEqual(result["checks"]["completion"]["status"], "failed")
        self.assertTrue(result["capabilities"]["metadata_extracted"])
        self.assertFalse(result["capabilities"]["playable"])

    def test_collection_sample_respects_a_configured_one_item_limit(self):
        with (
            patch.object(
                source,
                "enumerate_source",
                return_value=[("https://video.example/123", {}, {})],
            ) as enumerate_source,
            patch.object(source, "extract", return_value={}),
        ):
            checks.evaluate("https://video.example/channel", settings={"max_items": 1})
        self.assertEqual(enumerate_source.call_args.kwargs["limit"], 1)

    def test_worker_with_no_result_cannot_pass(self):
        child = MagicMock(returncode=0)
        child.communicate.return_value = (b"", None)
        with patch.object(checks.subprocess, "Popen", return_value=child):
            result = checks.check_url("https://video.example/123")
        self.assertEqual(result["result"], "failed")

    def test_decode_requires_frames_and_expected_sample_length(self):
        process = MagicMock(returncode=0, stdout="frame=5\nout_time_us=500000\n")
        with patch.object(checks.subprocess, "run", return_value=process):
            result = checks.decode("http://127.0.0.1/relay", 4)
        self.assertEqual(result["status"], "failed")
        process.stdout = "frame=40\nout_time_us=4000000\n"
        with patch.object(checks.subprocess, "run", return_value=process):
            self.assertEqual(
                checks.decode("http://127.0.0.1/relay", 4)["status"], "passed"
            )

    def test_reports_replace_prior_run_and_include_failure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            result = checks.finish(checks.new_report("https://video.example/123"))
            reporting.write_reports([result], directory)
            result["checks"]["seek"] = {
                "status": "failed",
                "summary": "Seeking failed.",
            }
            result = checks.finish(result)
            report = reporting.write_reports([result], directory)
            self.assertEqual(
                len(json.loads(Path(directory, "latest.json").read_text())["results"]),
                1,
            )
            self.assertIn("Seeking failed.", Path(directory, "latest.md").read_text())
            self.assertIn(
                report["generated_at"], Path(directory, "latest.md").read_text()
            )
            self.assertEqual(
                sorted(p.name for p in Path(directory).iterdir()),
                ["PROVIDERS.md", "latest.json", "latest.md"],
            )

    def test_relay_isolation_restores_catalog_configuration_and_caches(self):
        previous = (s.ROOT, s.CFG, s.FORMATS, s.URLS)
        with checks.isolated_relay():
            self.assertNotEqual(s.ROOT, previous[0])
            self.assertEqual(s.CFG["rex_token"], "")
            self.assertEqual(s.CFG["rex_url"], "http://127.0.0.1:1")
            self.assertIsNot(s.FORMATS, previous[2])
        self.assertEqual(s.ROOT, previous[0])
        self.assertIs(s.CFG, previous[1])
        self.assertIs(s.FORMATS, previous[2])
        self.assertIs(s.URLS, previous[3])

    def test_h265_extractor_codec_is_normalized_to_ffprobe_hevc_name(self):
        self.assertEqual(playback.codec("h265"), "hevc")

    def test_report_urls_strip_credentials_and_handle_malformed_inputs(self):
        text = checks.public_url(
            "https://name:private@video.example/watch?v=123&token=hidden"
        )
        self.assertEqual(text, "https://video.example/watch?v=123")
        self.assertEqual(checks.public_url("http://[invalid"), "[invalid URL]")

    def test_invalid_requests_are_rejected_before_spawning(self):
        for url in ["file:///etc/passwd", "https://user:password@video.example/123"]:
            with self.assertRaises(ValueError):
                checks.check_url(url)
        with self.assertRaises(ValueError):
            checks.check_url("https://video.example/123", seconds=1000)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs FFmpeg"
    )
    def test_real_http_relay_and_decoder_without_rex(self):
        """Synthetic media stays in RAM; only the isolated metadata DB is temporary."""
        media = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-f",
                "mp4",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout

        class Media(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(media)))
                self.end_headers()
                try:
                    self.wfile.write(media)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Media)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        video = info()
        video["duration"] = 3
        video["formats"][0].update(
            url=f"http://127.0.0.1:{server.server_port}/clip.mp4", width=160, height=90
        )
        try:
            with (
                patch.object(
                    source,
                    "enumerate_source",
                    return_value=[(video["webpage_url"], {}, {})],
                ),
                patch.object(source, "extract", return_value=video),
                patch.object(
                    checks,
                    "check_artwork",
                    return_value={"status": "passed", "summary": "Fixture artwork"},
                ),
                patch.object(
                    s, "rex", side_effect=AssertionError("Rex must never be called")
                ),
            ):
                result = checks.evaluate(video["webpage_url"], seconds=1)
            self.assertEqual(result["checks"]["relay"]["status"], "passed", result)
            self.assertEqual(
                result["checks"]["feed_metadata"]["status"], "passed", result
            )
            self.assertEqual(result["checks"]["decode"]["status"], "passed", result)
            self.assertTrue(result["capabilities"]["playable"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
