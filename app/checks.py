"""Read-only provider checks. Expensive work runs in a disposable child process.

A child serves the real HTTP relay against a temporary metadata DB, never calls
Rex, and decodes a few seconds to FFmpeg's null sink. The running catalog and URL
caches are never replaced by a check. Reports deliberately omit format URLs,
request headers, cookies and raw extractor/FFmpeg errors.
"""

import contextlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import source, playback
from app.config import validate_url, DEFAULT_OPTIONS, options

STAGES = (
    "discovery",
    "extraction",
    "metadata",
    "formats",
    "artwork",
    "relay",
    "feed_metadata",
    "decode",
    "seek",
)
ROOT = Path(__file__).resolve().parents[1]


def public_url(url):
    """Keep canonical video IDs in reports, discard opaque query credentials."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        host = "[" + host + "]" if ":" in host else host
        netloc = host + (":" + str(parsed.port) if parsed.port else "")
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parsed.query)
                if key in ("v", "list", "id")
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (ValueError, TypeError):
        return "[invalid URL]"


def new_report(url):
    """Start a safe URL report without copying temporary playback credentials."""
    return {
        "url": public_url(url),
        "provider": source.detect(url),
        "checks": {},
        "samples_tested": 0,
        "scope": "One representative video; no Rex or player-client check.",
    }


def finish(report):
    """Do not collapse playable-but-incomplete metadata into an unconditional pass."""
    for name in STAGES:
        report["checks"].setdefault(
            name, {"status": "skipped", "summary": "A prerequisite did not complete."}
        )
    statuses = [c["status"] for c in report["checks"].values()]
    if report.get("unavailable"):
        report["result"] = "unavailable"
    elif any(s in ("failed", "partial") for s in statuses):
        report["result"] = "partial" if "passed" in statuses else "failed"
    elif not any(s == "passed" for s in statuses):
        report["result"] = "skipped"
    else:
        report["result"] = "passed"
    report["capabilities"] = {
        "metadata_extracted": report["checks"]["extraction"]["status"] == "passed",
        "playable": report["checks"]["decode"]["status"] == "passed",
        "seekable": report["checks"]["seek"]["status"] == "passed",
        "feed_metadata_valid": report["checks"]["feed_metadata"]["status"] == "passed",
        "metadata_complete": report["checks"]["metadata"]["status"] == "passed",
    }
    report["level"] = (
        "unavailable"
        if report.get("unavailable")
        else (
            "decoded_with_gaps"
            if report["capabilities"]["playable"] and report["result"] != "passed"
            else (
                "decoded_sample"
                if report["capabilities"]["playable"]
                else (
                    "relay_readable"
                    if report["checks"]["relay"]["status"] == "passed"
                    else (
                        "metadata_only"
                        if report["capabilities"]["metadata_extracted"]
                        else "not_verified"
                    )
                )
            )
        )
    )
    return report


def error_summary(error):
    """Return actionable categories, never upstream signed URLs from error text."""
    if isinstance(error, source.Unavailable):
        return "The source positively reported unavailable/offline."
    status = re.search(r"HTTP (?:Error |error )?(\d{3})", str(error))
    if status:
        return "Upstream HTTP " + status[1] + "; anonymous access may be restricted."
    text = str(error)
    for pattern, summary in [
        (
            r"Unsupported URL",
            "No extractor or matching live plugin recognized this URL.",
        ),
        (
            r"[Ss]ign in|[Ll]ogin required|[Ll]og in|[Bb]ot challenge",
            "The provider requires sign-in or an access challenge for this item.",
        ),
        (
            r"not available in your country|geo.restrict",
            "The provider restricts this item by region.",
        ),
        (
            r"Premieres|not yet available|scheduled",
            "This item is scheduled and is not available to play yet.",
        ),
        (
            r"No anonymous, non-DRM video format",
            "The extractor offered no selectable anonymous non-DRM video format.",
        ),
    ]:
        if re.search(pattern, text, re.I):
            return summary
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "The operation exceeded its time limit."
    return "Operation failed (" + type(error).__name__ + ")."


@contextlib.contextmanager
def isolated_relay():
    """Used only in the worker (and tests); all relay state is restored on exit."""
    from app import service as s

    previous = (s.ROOT, s.CFG, s.FORMATS, s.URLS)
    server = thread = None
    with tempfile.TemporaryDirectory(prefix="rex-provider-check-") as directory:
        try:
            s.ROOT = Path(directory)
            s.FORMATS, s.URLS = {}, {}
            s.CFG = {
                "host": "127.0.0.1",
                "port": 0,
                "rex_url": "http://127.0.0.1:1",
                "rex_token": "",
                "source": "provider-check",
                "max_height": 1080,
                "live_buffer_mb": 8,
            }
            s.init_db()
            server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
            server.daemon_threads = True
            s.CFG.update(
                port=server.server_port,
                source_url=f"http://127.0.0.1:{server.server_port}",
            )
            s.local_url.cache_clear()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            yield s
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=3)
            s.ROOT, s.CFG, s.FORMATS, s.URLS = previous
            s.local_url.cache_clear()


def probe(url):
    """Read bounded relay headers and require an observed video stream."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-rw_timeout",
            "15000000",
            "-read_intervals",
            "%+#40",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError("Relay headers could not be decoded")
    streams = json.loads(result.stdout).get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("Relay contains no video stream")
    return streams


def decode(url, seconds, *, offset=None):
    """Decode a sample to null output and detect zero frames or early EOF."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-rw_timeout",
        "15000000",
    ]
    if offset is not None:
        command += ["-ss", str(offset)]
    command += [
        "-i",
        url,
        "-t",
        str(seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=55)
    frames = [int(n) for n in re.findall(r"^frame=(\d+)", result.stdout, re.M)]
    times = [
        int(n) / 1_000_000
        for n in re.findall(r"^out_time_us=(\d+)", result.stdout, re.M)
    ]
    duration = max(times, default=0)
    passed = (
        result.returncode == 0
        and max(frames, default=0) > 0
        and duration >= seconds * 0.8
    )
    return {
        "status": "passed" if passed else "failed",
        "summary": (
            "Decoded the requested sample."
            if passed
            else "Decoding failed or ended before the requested sample completed."
        ),
        "frames": max(frames, default=0),
        "seconds_decoded": round(duration, 3),
        "seconds_requested": seconds,
        "exit_code": result.returncode,
    }


def check_artwork(url):
    """Recognize an image from bounded response bytes without saving it."""
    if not url:
        return {"status": "partial", "summary": "No thumbnail supplied."}
    from PIL import ImageFile
    import requests

    # Read only enough to recognize an image; never save or dump the remote URL.
    with requests.get(url, stream=True, timeout=(8, 12)) as response:
        response.raise_for_status()
        parser = ImageFile.Parser()
        size = 0
        for chunk in response.iter_content(8192):
            size += len(chunk)
            parser.feed(chunk)
            if parser.image:
                return {
                    "status": "passed",
                    "summary": "Thumbnail is readable.",
                    "width": parser.image.width,
                    "height": parser.image.height,
                }
            if size >= 262144:
                break
    return {
        "status": "partial",
        "summary": "Thumbnail was not recognized within the bounded header check.",
    }


def evaluate(url, *, seconds=4, expected_kind=None, progress=None, settings=None):
    """Evaluate one URL using the same extraction and relay paths as the service."""
    report = new_report(url)
    settings = settings or {}

    def record(stage, status, summary, **values):
        report["checks"][stage] = dict(status=status, summary=summary, **values)
        if progress:
            progress(report)

    stage = "discovery"
    try:
        entries = list(
            source.enumerate_source(url, limit=min(2, settings.get("max_items", 2)))
        )
        if not entries:
            record(stage, "failed", "The collection returned no testable videos.")
            return finish(report)
        webpage, context, _ = entries[0]
        collection = bool(context.get("collection"))
        record(
            stage,
            "passed",
            (
                "Selected one representative video."
                if collection
                else "Recognized a single item."
            ),
            collection=collection,
            references_sampled=len(entries),
        )
        stage = "extraction"
        info = source.extract(webpage)
        if not isinstance(info, dict):
            raise ValueError("No metadata object")
        record(
            stage,
            "passed",
            "Extractor returned metadata.",
            extractor=info.get("extractor_key") or info.get("extractor"),
        )
        report["provider"] = source.provider(
            info.get("extractor_key") or info.get("extractor")
        )
        stage = "metadata"
        missing_required = [name for name in ("id", "title") if not info.get(name)]
        if not (info.get("webpage_url") or info.get("original_url")):
            missing_required.append("webpage_url")
        if missing_required:
            record(
                stage,
                "failed",
                "Required fields are missing.",
                missing=missing_required,
            )
            return finish(report)
        item = source.apply_settings(source.normalize(info, context), settings, context)
        missing = [
            name
            for name, value in [
                ("description", item["description"]),
                ("thumbnail", item["thumbnail"]),
                ("publication_date", item["published"]),
                (
                    "creator",
                    info.get("channel") or info.get("uploader") or context.get("name"),
                ),
            ]
            if not value
        ]
        if not item["is_live"] and not item["duration"]:
            missing.append("duration")
        found_kind = (
            "live"
            if item["is_live"]
            else "collection" if item["kind"] == "episode" else "video"
        )
        report["kind"] = found_kind
        expected_kind = expected_kind or (
            "live" if settings.get("type") == "live" else None
        )
        if expected_kind and found_kind != expected_kind:
            missing.append("expected_kind:" + expected_kind)
        record(
            stage,
            "partial" if missing else "passed",
            (
                "Metadata has gaps."
                if missing
                else "Required and descriptive fields are present."
            ),
            missing=missing,
        )
        report["item"] = {
            key: item[key]
            for key in (
                "key",
                "provider",
                "source_id",
                "title",
                "creator_name",
                "description",
                "published",
                "duration",
                "kind",
                "is_live",
                "season",
                "library_name",
            )
        }
        # Keep descriptions/report size bounded even for unexpectedly large pages.
        report["item"]["description"] = report["item"]["description"][:2000]
        stage = "formats"
        plan = playback.choose(info, settings.get("max_height", 1080))
        if settings.get("probe", "headers") == "headers":
            playback.inspect_layout(plan)
        record(
            stage,
            "passed",
            "Selected a relay-compatible format.",
            transport=plan["kind"],
        )
        stage = "artwork"
        try:
            report["checks"][stage] = check_artwork(item.get("thumbnail"))
        except Exception as error:
            record(stage, "partial", error_summary(error))
        with isolated_relay() as s:
            report["item"]["library_name"] = s.library_name(item)
            s.save_item(item, plan, url)
            with s.db() as con:
                row = con.execute(
                    "SELECT relay_path FROM items WHERE key=?", (item["key"],)
                ).fetchone()
            relay_url = s.local_url() + "/stream/" + row[0]
            stage = "relay"
            streams = probe(relay_url)
            record(
                stage,
                "passed",
                "Read video stream headers through the actual HTTP relay.",
                streams=streams,
            )
            stage = "feed_metadata"
            actual = {stream["index"]: stream for stream in streams}
            mismatches = []
            for expected in playback.media_info(plan)["streams"]:
                observed = actual.get(expected["index"], {})
                for field in ("codec_type", "codec_name"):
                    if expected.get(field) != observed.get(field):
                        mismatches.append(
                            f"Track {expected['index']} {field}: expected {expected.get(field)}, observed {observed.get(field)}"
                        )
            record(
                stage,
                "failed" if mismatches else "passed",
                (
                    "Declared tracks disagree with observed media."
                    if mismatches
                    else "Declared track indices and codecs match the relay."
                ),
                mismatches=mismatches,
            )
            stage = "decode"
            target = (
                min(seconds, float(item["duration"]))
                if item["duration"] and not item["is_live"]
                else seconds
            )
            report["checks"][stage] = decode(relay_url, target)
            report["samples_tested"] = 1
            if progress:
                progress(report)
            stage = "seek"
            if item["is_live"]:
                record(stage, "skipped", "Live playback has no finite seek history.")
            elif not item["duration"] or float(item["duration"]) < 10:
                record(stage, "skipped", "No sufficient finite duration was supplied.")
            elif report["checks"]["decode"]["status"] != "passed":
                record(stage, "skipped", "Initial playback must pass before seeking.")
            else:
                offset = min(60, float(item["duration"]) / 2)
                report["checks"][stage] = dict(
                    decode(relay_url, min(2, seconds), offset=offset), offset=offset
                )
    except Exception as error:
        if isinstance(error, source.Unavailable):
            report["unavailable"] = True
        record(stage, "failed", error_summary(error), error_type=type(error).__name__)
    return finish(report)


def check_url(url, *, seconds=4, timeout=180, expected_kind=None, settings=None):
    """Public entry point: a fresh process isolates the service and bounds children."""
    validate_url(url)
    if settings is not None:
        if not isinstance(settings, dict) or set(settings) - (
            set(DEFAULT_OPTIONS)
            | {"site", "name", "library", "movie_library", "live_library"}
        ):
            raise ValueError("Invalid source settings")
        options({**DEFAULT_OPTIONS, **settings}, "check settings")
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not 1 <= seconds <= 15
    ):
        raise ValueError("seconds must be between 1 and 15")
    if not 10 <= timeout <= 300:
        raise ValueError("timeout must be between 10 and 300 seconds")
    command = [sys.executable, "-m", "app.checks"]
    request = json.dumps(
        {
            "url": url,
            "seconds": seconds,
            "expected_kind": expected_kind,
            "settings": settings,
        }
    ).encode()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(input=request, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
    report = new_report(url)
    found = False
    for line in output.decode(errors="replace").splitlines():
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict) and "checks" in candidate:
                report = candidate
                found = True
        except ValueError:
            pass
    if timed_out or process.returncode != 0 or not found:
        report["checks"]["completion"] = {
            "status": "failed",
            "summary": (
                "Check exceeded its overall deadline."
                if timed_out
                else "Check worker exited unexpectedly."
            ),
        }
    return finish(report)


if __name__ == "__main__":
    import logging

    logging.disable(logging.CRITICAL)
    os.environ.pop("REX_TOKEN", None)

    def emit(report):
        print(json.dumps(report), flush=True)

    request = json.load(sys.stdin)
    request["url"] = validate_url(request["url"])
    result = evaluate(**request, progress=emit)
    emit(result)
