"""Opt-in public playback checks. No site needs user credentials or cached media.

Use --rex-bin to test in an isolated, temporary Rex process as well. A currently
live, benign programming stream can be passed via --live-url; it is never required
by unit tests. Reports contain canonical page URLs, never signed playback URLs.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playback
import providers
import service as s

# Public educational/product demonstrations and an official Blender render example.
EXAMPLES = {
    "dash-demo": "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd",
    "youtube": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    "vimeo": "https://vimeo.com/1084537",
    "dailymotion": "https://www.dailymotion.com/video/x7bs2wf",
    "peertube": "https://framatube.org/videos/watch/9c9de5e8-0a1e-484a-b099-e80766180a6d",
    "bilibili": "https://www.bilibili.com/video/BV15z4y1F7tK/",
    "tiktok": "https://www.tiktok.com/@astrosamantha/video/7166258449645407493",
    "twitch": "https://www.twitch.tv/videos/2905866465",
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rex_fixture(binary, directory):
    """Start a disposable catalog; never point these probes at a user's libraries."""
    port = free_port()
    catalog = directory / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "format": 1,
                "machineIdentifier": "web-video-tests",
                "friendlyName": "Web Video Checks",
                "sections": [],
                "items": [],
            }
        )
    )
    env = {
        **os.environ,
        "REX_DIRECT_URL": "off",
        "REX_TRANSCODER": shutil.which("ffmpeg"),
        "REX_TRANSCODE_HW": "none",
        "REX_TRANSCODE_DIR": str(directory / "transcode"),
    }
    log = (directory / "rex.log").open("w")
    process = subprocess.Popen(
        [
            binary,
            "--catalog",
            str(catalog),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--assert-claimed",
            "--no-state",
            "--gdm",
            "off",
        ],
        cwd=directory,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            if s.requests.get(base + "/identity", timeout=0.2).status_code == 200:
                return process, base, log
        except s.requests.RequestException:
            pass
        if process.poll() is not None:
            raise RuntimeError("Disposable Rex did not start")
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("Disposable Rex startup timed out")


def resolve_example(pair):
    name, url = pair
    if url.endswith("/videos"):
        url, ctx, entry = next(providers.enumerate_source(url, limit=1))
    info = providers.extract(url)
    return name, url, info, playback.inspect_layout(playback.choose(info))


def inspect_stream(url):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-read_intervals",
            "%+#30",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=80,
    )
    if result.returncode:
        raise RuntimeError("ffprobe failed: " + result.stderr[-250:])
    streams = json.loads(result.stdout).get("streams", [])
    assert {"video", "audio"} <= {
        x.get("codec_type") for x in streams
    }, "Audio/video missing"
    return streams


def rex_playback(row, live=False):
    s.publish(row)
    with s.db() as con:
        key = con.execute(
            "SELECT rating_key FROM items WHERE key=?", (row["key"],)
        ).fetchone()[0]
    if live:
        meta = s.rex("GET", "/library/metadata/" + key)["MediaContainer"]["Metadata"][0]
        part = meta["Media"][0]["Part"][0]["key"]
        # Test continuous playback through the actual Part endpoint, not just source discovery.
        url = s.CFG["rex_url"] + part
        duration = 12
        ident = None
    else:
        ident = uuid.uuid4().hex
        params = {
            "path": "/library/metadata/" + key,
            "mediaIndex": 0,
            "partIndex": 0,
            "protocol": "dash",
            "directPlay": 0,
            "directStream": 1,
            "directStreamAudio": 1,
            "session": ident,
            "X-Plex-Client-Identifier": ident,
            "X-Plex-Session-Identifier": ident,
            "X-Plex-Platform": "Chrome",
            "X-Plex-Client-Profile-Name": "Chrome",
            "X-Plex-Product": "Plex Web",
        }
        decision = s.rex("GET", "/video/:/transcode/universal/decision", params=params)
        assert decision["MediaContainer"].get("generalDecisionCode") in (1000, 1001)
        result = s.requests.get(
            s.CFG["rex_url"] + "/video/:/transcode/universal/start.mpd",
            params=params,
            timeout=40,
        )
        result.raise_for_status()
        url = result.url
        duration = 3
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "info",
                "-i",
                url,
                "-t",
                str(duration),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=100,
        )
        assert result.returncode == 0 and re.search(
            r"frame=\s*[1-9]\d*", result.stderr
        ), ("Rex playback did not decode video/audio: " + result.stderr[-300:])
        return (
            "decoded "
            + str(duration)
            + "s through "
            + ("Rex Part (live)" if live else "Rex DASH")
        )
    finally:
        if ident:
            s.rex("GET", "/video/:/transcode/universal/stop", params={"session": ident})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rex-bin", help="Optional built Rex executable for isolated playback tests"
    )
    parser.add_argument(
        "--site", action="append", help="Run only named providers; repeatable"
    )
    parser.add_argument(
        "--live-url",
        help="A currently live, safe-for-work programming/educational stream",
    )
    args = parser.parse_args()
    selected = {k: v for k, v in EXAMPLES.items() if not args.site or k in args.site}
    if args.live_url:
        selected["live-experiment"] = args.live_url
    failures = 0
    with tempfile.TemporaryDirectory(prefix="rex-youtube-probes-") as temp:
        s.ROOT = Path(temp)
        s.init_db()
        server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        server.daemon_threads = True
        s.CFG = {
            **s.CFG,
            "port": server.server_port,
            "source_url": f"http://127.0.0.1:{server.server_port}",
            "source": "web-video-tests",
            "rex_token": "",
        }
        s.local_url.cache_clear()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        rex = None
        rex_log = None
        try:
            if args.rex_bin:
                rex, base, rex_log = rex_fixture(
                    str(Path(args.rex_bin).resolve()), Path(temp)
                )
                s.CFG["rex_url"] = base
                s.setup_rex()
            for name, url in selected.items():
                report = {"provider": name, "url": url}
                try:
                    _, canonical, info, plan = resolve_example((name, url))
                    item = providers.normalize(info)
                    s.save_item(item, plan, canonical)
                    with s.db() as con:
                        row = dict(
                            con.execute(
                                "SELECT * FROM items WHERE key=?", (item["key"],)
                            ).fetchone()
                        )
                    report.update(
                        title=item["title"],
                        transport=plan["kind"],
                        streams=inspect_stream(
                            s.local_url() + "/stream/" + row["relay_path"]
                        ),
                    )
                    actual = {x["index"]: x["codec_type"] for x in report["streams"]}
                    for stream in playback.media_info(plan)["streams"]:
                        assert (
                            actual.get(stream["index"]) == stream["codec_type"]
                        ), "Feed track index disagrees with relay"
                    if rex:
                        report["rex"] = rex_playback(row, item["is_live"])
                    report["result"] = "passed"
                except Exception as error:
                    # Error tails can mention signed URLs; reports intentionally omit them.
                    failures += 1
                    report.update(result="failed", error_type=type(error).__name__)
                    print(name + ": " + str(error)[:200], file=sys.stderr, flush=True)
                print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            if rex:
                rex.terminate()
                rex.wait(timeout=15)
                rex_log.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
