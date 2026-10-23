"""Opt-in, memory-only stall check: a native HTTP reader survives an 18s outage.

Generate a tiny synthetic clip in RAM, interrupt its producer, and decode it over
HTTP with Rex's 15-second input timeout. No media files or external sites are used.
"""

import io
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live


def main():
    fixture = subprocess.run(
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
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    split = len(fixture) // (188 * 2) * 188
    plan = {
        "video": {"url": "https://fixture.invalid/live.m3u8", "protocol": "m3u8_native"}
    }

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            data = super().read(size)
            if not data:
                raise OSError("injected source disconnect")
            return data

    class Delayed(io.BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                time.sleep(18)
            return super().read(size)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Connection", "close")
            self.end_headers()
            relay = live.LiveRelay(plan, lambda: plan)
            try:
                for data in relay.chunks():
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                relay.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        with patch.object(
            live,
            "open_reader",
            side_effect=[Interrupted(fixture[:split]), Delayed(fixture[split:])],
        ):
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-rw_timeout",
                    "15000000",
                    "-i",
                    f"http://127.0.0.1:{server.server_port}/stream",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=50,
            )
        errors = result.stderr.decode()
        frames = [int(n) for n in re.findall(r"frame=\s*(\d+)", errors)]
        assert result.returncode == 0 and frames and max(frames) >= 59, errors[-500:]
        assert "Connection timed out" not in errors, errors[-500:]
        print(
            f"PASS: decoded all {max(frames)} frames and audio across an 18s outage "
            f"with a 15s HTTP read timeout ({time.monotonic()-started:.1f}s wall time); no media files"
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
