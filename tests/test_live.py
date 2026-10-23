"""Live recovery never closes the muxer's input during a temporary source stall."""

import io
import unittest
from unittest.mock import MagicMock, patch

from app import live


class Sink(io.BytesIO):
    def close(self):
        self.ended = True


class Reader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def read(self, size):
        item = next(self.chunks, b"")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class LiveTests(unittest.TestCase):
    def plan(self):
        return {
            "video": {
                "url": "https://cdn.example/live.m3u8",
                "protocol": "m3u8_native",
                "http_headers": {"Referer": "https://channel.example/"},
            }
        }

    def test_buffer_is_bounded_and_hls_retries_are_enabled(self):
        with patch.object(live, "HLSStream") as stream:
            live.open_reader(self.plan(), 16)
        session = stream.call_args.args[0]
        self.assertEqual(session.get_option("ringbuffer-size"), 16 * 1024 * 1024)
        self.assertEqual(session.get_option("stream-segment-threads"), 2)
        self.assertEqual(session.get_option("stream-segment-attempts"), 5)
        self.assertEqual(
            stream.call_args.kwargs["headers"]["Referer"], "https://channel.example/"
        )

    def test_temporary_failure_refreshes_without_sending_eof(self):
        first = Reader([b"first", IOError("temporary transport failure")])
        second = Reader([b"second", b""])
        refresh = MagicMock(return_value=self.plan())
        relay = live.LiveRelay(self.plan(), refresh)
        relay.process = MagicMock()
        relay.process.poll.return_value = None
        relay.process.stdin = Sink()
        with (
            patch.object(live, "open_reader", side_effect=[first, second]),
            patch.object(relay.stop, "wait", return_value=False),
            self.assertLogs("rex-youtube", level="WARNING"),
        ):
            relay._pump()
        self.assertEqual(relay.process.stdin.getvalue(), b"firstsecond")
        self.assertTrue(relay.process.stdin.ended)
        self.assertTrue(first.closed and second.closed)
        refresh.assert_called_once()

    def test_separate_live_audio_is_muxed_and_shares_buffer_budget(self):
        plan = self.plan()
        plan["audio"] = {"url": "https://cdn.example/audio.m3u8", "protocol": "m3u8"}
        with (
            patch.object(live, "HLSStream") as stream,
            patch.object(live, "MuxedStream") as mux,
        ):
            reader = live.open_reader(plan, 16)
        self.assertEqual(stream.call_count, 2)
        self.assertEqual(
            stream.call_args.args[0].get_option("ringbuffer-size"), 8 * 1024 * 1024
        )
        self.assertEqual(mux.call_args.kwargs["maps"], ["0:v:0", "1:a:0"])
        self.assertEqual(mux.call_args.kwargs["format"], "mpegts")
        self.assertIs(reader, mux.return_value.open.return_value)

    def test_offline_confirmation_stops_reconnect(self):
        from app.source import Unavailable

        relay = live.LiveRelay(
            self.plan(), MagicMock(side_effect=Unavailable("offline"))
        )
        relay.process = MagicMock()
        relay.process.poll.return_value = None
        relay.process.stdin = Sink()
        with (
            patch.object(live, "open_reader", side_effect=IOError("dropped")) as source,
            patch.object(relay.stop, "wait", return_value=False),
            self.assertLogs("rex-youtube", level="WARNING"),
        ):
            relay._pump()
        self.assertEqual(source.call_count, 1)
        self.assertTrue(relay.process.stdin.ended)

    def test_transport_heartbeat_is_legal_null_ts_without_fake_frames(self):
        self.assertEqual(len(live.NULL_TS) % 188, 0)
        for offset in range(0, len(live.NULL_TS), 188):
            packet = live.NULL_TS[offset : offset + 188]
            self.assertEqual(packet[0], 0x47)
            self.assertEqual(((packet[1] & 0x1F) << 8) | packet[2], 0x1FFF)

    def test_cancelled_reader_does_not_reconnect(self):
        relay = live.LiveRelay(self.plan(), MagicMock())
        relay.stop.set()
        relay.process = MagicMock()
        relay.process.stdin = Sink()
        with patch.object(live, "open_reader") as source:
            relay._pump()
        source.assert_not_called()
        relay.refresh.assert_not_called()

    def test_real_copy_mux_recovers_a_stall_without_any_media_files(self):
        import shutil
        import subprocess

        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for the in-memory media check")
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
                "4",
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
            capture_output=True,
            check=True,
        ).stdout
        split = (len(fixture) // (188 * 2)) * 188
        first = Reader([fixture[:split], IOError("temporary source outage")])
        second = Reader([fixture[split:], b""])
        relay = live.LiveRelay(self.plan(), MagicMock(return_value=self.plan()))
        with (
            patch.object(live, "open_reader", side_effect=[first, second]),
            self.assertLogs("rex-youtube", level="WARNING"),
        ):
            payload = b"".join(relay.chunks())
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            input=payload,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-500:])
        self.assertIn(b"frame=", result.stderr)
        self.assertFalse(relay.thread.is_alive())
        self.assertIsNotNone(relay.process.poll())
