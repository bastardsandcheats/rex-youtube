"""Exercise HTTP relay behavior with a local listener and mocked YouTube responses."""

import http.client
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import requests
import service as s


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
