"""Small memory-only live read-ahead; Streamlink owns HLS segment retries."""

import logging
import os
import select
import subprocess
import threading
import time

# Both libraries wrap urllib3 URL normalization. Load yt-dlp first so the
# Streamlink wrapper delegates regex methods instead of hiding them.
from app.source import Unavailable

import streamlink
from streamlink.stream.hls import HLSStream
from streamlink.stream.http import HTTPStream
from streamlink.stream.ffmpegmux import MuxedStream

from app import playback

LOG = logging.getLogger("rex-youtube")
# Third-party transport errors can include signed URLs; our recovery logs omit them.
logging.getLogger("streamlink").setLevel(logging.CRITICAL)
# Legal MPEG-TS stuffing, not video frames or a fabricated playback timestamp.
# A heartbeat keeps Rex's HTTP read alive while real media is temporarily late.
NULL_TS = (b"\x47\x1f\xff\x10" + b"\xff" * 184) * 7
LIMIT = threading.BoundedSemaphore(4)


def open_reader(plan, buffer_mb):
    """Open bounded Streamlink readers, combining separate tracks through FIFO pipes."""
    session = streamlink.Streamlink()
    # Separate live renditions share the same read-ahead budget. Streamlink's
    # muxer uses FIFO pipes, never regular media files, to combine their bytes.
    tracks = [plan["video"]] + ([plan["audio"]] if plan.get("audio") else [])
    session.set_option("ringbuffer-size", buffer_mb * 1024 * 1024 // len(tracks))
    session.set_option("stream-segment-threads", 2)
    session.set_option("stream-segment-attempts", 5)
    session.set_option("stream-segment-timeout", 8)
    session.set_option("hls-playlist-reload-attempts", 5)
    session.set_option("stream-timeout", 30)
    streams = []
    for fmt in tracks:
        cls = HLSStream if "m3u8" in (fmt.get("protocol") or "") else HTTPStream
        stream = cls(session, fmt["url"], headers=playback.headers(plan, fmt))
        if len(tracks) == 2 and "m3u8" in (fmt.get("protocol") or ""):
            # YouTube uses ID3 versions Streamlink's packed-audio parser rejects.
            # Leave those tags intact for FFmpeg's demuxer instead of stripping them.
            stream.parse_packed_audio = False
        streams.append(stream)
    if len(streams) == 2:
        return MuxedStream(
            session, *streams, format="mpegts", maps=["0:v:0", "1:a:0"]
        ).open()
    return streams[0].open()


class LiveRelay:
    """One bounded read-ahead reader and copy-muxer per active playback request.

    Reconnecting the reader leaves the muxer's stdin open, so a transient source
    failure is not sent downstream as EOF. Cancellation closes all readers/pipes.
    """

    def __init__(self, plan, refresh, buffer_mb=16):
        self.plan = plan
        self.refresh = refresh
        self.buffer_mb = max(1, min(64, int(buffer_mb)))
        self.stop = threading.Event()
        self.reader = None
        self.thread = None
        self.process = None
        self.error = None
        self.lock = threading.Lock()

    def _pump(self):
        failures = 0
        outage = None
        try:
            while not self.stop.is_set():
                try:
                    reader = open_reader(self.plan, self.buffer_mb)
                    with self.lock:
                        self.reader = reader
                    try:
                        while not self.stop.is_set():
                            chunk = reader.read(32768)
                            if not chunk:
                                return  # An ended HLS playlist is a real EOF.
                            pending = memoryview(chunk)
                            while pending and not self.stop.is_set():
                                written = self.process.stdin.write(pending)
                                if not written:
                                    raise BrokenPipeError(
                                        "Live muxer stopped accepting data"
                                    )
                                pending = pending[written:]
                            failures = 0
                            outage = None
                    finally:
                        reader.close()
                        with self.lock:
                            self.reader = None
                except (BrokenPipeError, OSError) as error:
                    if self.stop.is_set() or self.process.poll() is not None:
                        return
                    self.error = error
                except Exception as error:
                    self.error = error
                failures += 1
                outage = outage or time.monotonic()
                if failures > 3 or time.monotonic() - outage >= 60:
                    LOG.warning("Live source recovery exhausted; ending playback")
                    return
                LOG.warning(
                    "Live source stalled; refreshing playback URL (attempt %s/3)",
                    failures,
                )
                if self.stop.wait(min(failures, 3)):
                    return
                try:
                    self.plan = self.refresh()
                except Exception as error:
                    # An offline confirmation ends the broadcast; network errors retry.
                    if isinstance(error, Unavailable):
                        LOG.info("Live broadcast has ended")
                        return
                    self.error = error
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def chunks(self):
        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-fflags",
                "+genpts+discardcorrupt",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-f",
                "mpegts",
                "-muxdelay",
                "0",
                "-flush_packets",
                "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.thread = threading.Thread(
            target=self._pump, daemon=True, name="live-buffer"
        )
        self.thread.start()
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.process.stdout], [], [], 1)
                if readable:
                    chunk = os.read(self.process.stdout.fileno(), 188 * 128)
                    if not chunk:
                        status = self.process.wait(timeout=3)
                        if status and not self.stop.is_set():
                            LOG.warning("Live copy-muxer exited with status %s", status)
                        break
                    yield chunk
                else:
                    yield NULL_TS
        finally:
            self.close()

    def close(self):
        self.stop.set()
        with self.lock:
            reader = self.reader
        if reader:
            reader.close()
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
            if not self.process.stdin.closed:
                self.process.stdin.close()
        if self.thread:
            self.thread.join(timeout=2)
