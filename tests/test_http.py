"""Exercise HTTP relay behavior with a local listener and mocked YouTube responses."""

import http.client
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import requests
from app import service as s


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.ident = "abcdefghijk"
        self.token = "a" * 32
        self.path = f"/hls/{self.ident}/{self.token}.ts"
        self.mapping = patch.dict(
            s.URLS,
            {
                (self.ident, self.token): (
                    "https://media.googlevideo.com/index.m3u8/segment.ts",
                    {},
                )
            },
            clear=True,
        )
        self.mapping.start()

    def tearDown(self):
        self.mapping.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def response(self, content, status=200):
        upstream = MagicMock()
        upstream.__enter__.return_value = upstream
        upstream.status_code = status
        upstream.headers = {
            "Content-Type": "video/MP2T",
            "Content-Length": str(len(content)),
        }
        upstream.iter_content.return_value = iter([content])
        return upstream

    def test_binary_segments_with_m3u8_in_the_url_are_not_decoded_as_text(self):
        payload = b"\x00\xff\xfe\x80binary-media"
        with patch.object(s.requests, "get", return_value=self.response(payload)):
            with urlopen(self.base + self.path, timeout=5) as result:
                self.assertEqual(result.read(), payload)
                self.assertEqual(result.headers["Content-Type"], "video/MP2T")

    def test_range_is_forwarded_and_partial_response_headers_are_preserved(self):
        upstream = self.response(b"part", 206)
        upstream.headers["Content-Range"] = "bytes 10-13/100"
        with patch.object(s.requests, "get", return_value=upstream) as fetch:
            with urlopen(
                Request(self.base + self.path, headers={"Range": "bytes=10-13"}),
                timeout=5,
            ) as result:
                self.assertEqual(result.status, 206)
                self.assertEqual(result.headers["Content-Range"], "bytes 10-13/100")
                self.assertEqual(result.read(), b"part")
            self.assertEqual(fetch.call_args.kwargs["headers"]["Range"], "bytes=10-13")

    def test_midstream_failure_closes_instead_of_appending_an_html_error(self):
        upstream = self.response(b"first")
        upstream.headers["Content-Length"] = "100"

        def interrupted():
            yield b"first"
            raise requests.exceptions.Timeout("upstream timed out")

        upstream.iter_content.return_value = interrupted()
        with (
            patch.object(s.requests, "get", return_value=upstream),
            self.assertLogs(s.LOG, level="WARNING"),
        ):
            with urlopen(self.base + self.path, timeout=5) as result:
                with self.assertRaises(http.client.IncompleteRead) as error:
                    result.read()
                self.assertEqual(error.exception.partial, b"first")


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        self.server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_check_uses_worker_and_writes_reports_without_publishing(self):
        import json
        from app import checks, reporting

        report = checks.finish(checks.new_report("https://video.example/123"))
        original_root, original_config = s.ROOT, s.CFG
        with (
            patch.object(checks, "check_url", return_value=report) as check,
            patch.object(reporting, "write_reports") as write,
            patch.object(s, "rex", side_effect=AssertionError("No Rex calls")),
        ):
            result = requests.post(
                self.base + "/api/providers/check",
                json={"url": "https://video.example/123", "seconds": 2},
                timeout=5,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.text)["url"], "https://video.example/123")
        check.assert_called_once_with("https://video.example/123", seconds=2)
        write.assert_called_once()
        self.assertIs(s.CFG, original_config)
        self.assertEqual(s.ROOT, original_root)

    def test_provider_inventory_exposes_the_custom_handlers(self):
        result = requests.get(self.base + "/api/providers", timeout=5).json()
        youtube = next(p for p in result["providers"] if p["key"] == "youtube")
        self.assertEqual(
            set(youtube["handlers"]), {"collection_url", "live_url", "entry_url"}
        )
        self.assertIn(
            "targets",
            next(p for p in result["providers"] if p["key"] == "twitch")["handlers"],
        )

    def test_invalid_or_oversized_requests_do_not_start_checks(self):
        from app import checks

        with patch.object(checks, "check_url") as check:
            for body in [
                {"url": "file:///etc/passwd"},
                {"url": "https://video.example", "seconds": 1000},
                {"url": "https://video.example", "seconds": True},
                [],
                {"url": "https://video.example/" + "a" * 9000},
            ]:
                self.assertEqual(
                    requests.post(
                        self.base + "/api/providers/check", json=body, timeout=5
                    ).status_code,
                    400,
                )
        check.assert_not_called()

    def test_remote_check_requires_configured_token(self):
        from app import checks

        remote = MagicMock(is_loopback=False)
        with (
            patch.object(s.ipaddress, "ip_address", return_value=remote),
            patch.dict(s.CFG, rex_token="fixture-admin-token"),
            patch.dict(s.os.environ, {"REX_TOKEN": ""}),
            patch.object(checks, "check_url") as check,
        ):
            result = requests.post(
                self.base + "/api/providers/check",
                json={"url": "https://video.example"},
                timeout=5,
            )
            self.assertEqual(result.status_code, 403)
            check.assert_not_called()

    def test_busy_checker_refuses_extra_work(self):
        from app import checks

        with (
            patch.object(s, "CHECK_LIMIT") as limit,
            patch.object(checks, "check_url") as check,
        ):
            limit.acquire.return_value = False
            result = requests.post(
                self.base + "/api/providers/check",
                json={"url": "https://video.example"},
                timeout=5,
            )
            self.assertEqual(result.status_code, 429)
            check.assert_not_called()
