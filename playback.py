"""Small playback plans and HTTP/HLS/DASH manifest rewriting; no media files."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

DASH = "urn:mpeg:dash:schema:mpd:2011"
ET.register_namespace("", DASH)


def choose(info, max_height=1080):
    """Prefer quality, then H.264 compatibility; retain the site's simplest transport."""
    formats = [
        f
        for f in info.get("formats", [])
        if f.get("url")
        and not f.get("has_drm")
        and f.get("protocol") not in ("mhtml", "f4m")
    ]
    video = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and (f.get("height") or 0) <= max_height
    ]
    audio = [
        f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"
    ]
    if not video:
        raise ValueError("No anonymous, non-DRM video format is available")

    def rank(f):
        return (
            str(f.get("vcodec")).startswith("avc"),
            f.get("height") or 0,
            f.get("acodec") not in (None, "none"),
            f.get("tbr") or 0,
        )

    v = max(video, key=rank)
    protocol = v.get("protocol") or ""
    a = None
    # HLS video variants can reference an external audio group even when yt-dlp
    # leaves acodec unset. Choose that rendition before synthesizing the master.
    if audio and (v.get("acodec") in (None, "none")):
        compatible = [
            f
            for f in audio
            if ("m3u8" in (f.get("protocol") or "")) == ("m3u8" in protocol)
        ]
        if compatible:
            a = max(
                compatible,
                key=lambda f: (
                    "original" in str(f.get("format_note")),
                    str(f.get("acodec")).startswith("mp4a"),
                    f.get("abr") or f.get("tbr") or 0,
                ),
            )
    kind = (
        "hls" if "m3u8" in protocol else "dash" if a or "dash" in protocol else "http"
    )
    live = bool(info.get("is_live")) and not str(
        info.get("extractor_key", "")
    ).lower().startswith("twitchvod")
    return {
        "kind": "live" if live else kind,
        "video": v,
        "audio": a,
        "headers": info.get("http_headers") or {},
        "duration": info.get("duration")
        or sum(f.get("duration") or 0 for f in v.get("fragments", [])),
        "live": live,
    }


def codec(value, audio=False):
    value = str(value or "")
    for prefix, name in [
        ("avc", "h264"),
        ("hvc", "hevc"),
        ("hev", "hevc"),
        ("vp09", "vp9"),
        ("av01", "av1"),
        ("mp4a", "aac"),
    ]:
        if value.startswith(prefix):
            return name
    return (
        value.split(".")[0] if value and value != "none" else "aac" if audio else "h264"
    )


def media_info(plan):
    """Use extractor metadata; stream indices match the manifest we actually serve."""
    v, a, kind = plan["video"], plan.get("audio"), plan["kind"]
    vi, ai = (1, 0) if kind == "hls" and a else (0, 1)
    video = {
        "index": vi,
        "codec_type": "video",
        "codec_name": codec(v.get("vcodec")),
        "width": v.get("width"),
        "height": v.get("height"),
        "pix_fmt": "yuv420p",
        "avg_frame_rate": str(v.get("fps") or 30),
        "bit_rate": str(int((v.get("vbr") or v.get("tbr") or 2000) * 1000)),
    }
    avc = re.fullmatch(r"avc[13]\.([0-9a-fA-F]{6})", str(v.get("vcodec")))
    if avc:
        video.update(
            profile={"42": "Baseline", "4d": "Main", "64": "High"}.get(
                avc[1][:2].lower(), "High"
            ),
            level=int(avc[1][-2:], 16),
        )
    streams = [video]
    if a or v.get("acodec") not in (None, "none") or kind in ("hls", "live"):
        sound = a or v
        streams.append(
            {
                "index": ai,
                "codec_type": "audio",
                "codec_name": codec(sound.get("acodec"), True),
                "channels": sound.get("audio_channels") or 2,
                "sample_rate": str(sound.get("asr") or 44100),
                "bit_rate": str(int((sound.get("abr") or 128) * 1000)),
                "tags": {"language": sound.get("language") or "und"},
            }
        )
    container = {"hls": "hls", "dash": "dash", "live": "mpegts"}.get(
        kind, "webm" if v.get("ext") == "webm" else "mov,mp4,m4a,3gp,3g2,mj2"
    )
    fmt = {"format_name": container}
    if plan["duration"]:
        fmt["duration"] = str(plan["duration"])
    return {"format": fmt, "streams": streams}


def extension(plan):
    return {"hls": "m3u8", "dash": "mpd", "live": "ts"}.get(
        plan["kind"], "webm" if plan["video"].get("ext") == "webm" else "mp4"
    )


def headers(plan, fmt):
    return {**plan.get("headers", {}), **fmt.get("http_headers", {})}


def hls_master(plan, relay):
    """Our controlled two-rendition master guarantees known FFmpeg stream order."""
    v, a = plan["video"], plan.get("audio")
    video = relay(v["url"], headers(plan, v), "m3u8")
    if not a:
        return None, video
    audio = relay(a["url"], headers(plan, a), "m3u8")
    rate = int(((v.get("tbr") or 2000) + (a.get("tbr") or 128)) * 1000)
    text = f'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",DEFAULT=YES,AUTOSELECT=YES,URI="{audio}"\n#EXT-X-STREAM-INF:BANDWIDTH={rate},RESOLUTION={v["width"]}x{v["height"]},CODECS="{v.get("vcodec") or "avc1.640028"},mp4a.40.2",AUDIO="audio"\n{video}\n'
    return text, None


def rewrite_hls(text, base, request_headers, relay):
    lines = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY") and "com.apple.streamingkeydelivery" in line:
            raise ValueError("DRM-protected HLS is not supported")
        if line and not line.startswith("#"):
            ext = (
                "m3u8" if urlsplit(urljoin(base, line)).path.endswith(".m3u8") else "ts"
            )
            line = relay(urljoin(base, line), request_headers, ext)
        elif 'URI="' in line:
            ext = (
                "mp4"
                if line.startswith("#EXT-X-MAP")
                else (
                    "m3u8"
                    if line.startswith(("#EXT-X-MEDIA", "#EXT-X-I-FRAME"))
                    else "key"
                )
            )
            line = re.sub(
                r'URI="([^"]+)"',
                lambda m: 'URI="'
                + relay(urljoin(base, m[1]), request_headers, ext)
                + '"',
                line,
            )
        lines.append(line)
    return "\n".join(lines) + "\n"


def dash_manifest(plan, relay):
    """Build one video + optional audio representation from yt-dlp's fragment list.

    A progressive audio/video pair can also be represented as two full-file ISO
    segments. Byte ranges still pass through; no local muxer or media cache is used.
    """

    def node(parent, tag, **attrs):
        return ET.SubElement(
            parent, "{" + DASH + "}" + tag, {k: str(v) for k, v in attrs.items()}
        )

    attrs = {
        "type": "static",
        "profiles": "urn:mpeg:dash:profile:isoff-main:2011",
        "minBufferTime": "PT2S",
        "mediaPresentationDuration": f'PT{plan["duration"]}S',
    }
    root = ET.Element("{" + DASH + "}MPD", attrs)
    period = node(root, "Period", start="PT0S")
    for index, fmt in enumerate(
        [plan["video"]] + ([plan["audio"]] if plan.get("audio") else [])
    ):
        audio = index == 1
        adaptation = node(
            period, "AdaptationSet", contentType="audio" if audio else "video"
        )
        container = "webm" if fmt.get("ext") == "webm" else "mp4"
        rep = node(
            adaptation,
            "Representation",
            id=index,
            mimeType=("audio/" if audio else "video/") + container,
            codecs=fmt.get("acodec" if audio else "vcodec")
            or ("mp4a.40.2" if audio else "avc1.640028"),
            bandwidth=int((fmt.get("tbr") or 128) * 1000),
        )
        request_headers = headers(plan, fmt)
        fragments = list(fmt.get("fragments") or [])
        base = fmt.get("fragment_base_url") or fmt["url"]
        if not fragments:
            fragments = [{"url": fmt["url"], "duration": plan["duration"]}]
        segments = node(rep, "SegmentList", timescale=1000)
        timeline = node(segments, "SegmentTimeline")
        for n, fragment in enumerate(fragments):
            url = urljoin(base, fragment.get("url") or fragment.get("path") or "")
            target = relay(
                url, request_headers, "webm" if container == "webm" else "mp4"
            )
            duration = fragment.get("duration")
            if n == 0 and not duration and len(fragments) > 1:
                node(segments, "Initialization", sourceURL=target)
                continue
            node(
                timeline, "S", d=max(1, int((duration or plan["duration"] or 1) * 1000))
            )
            entry = node(segments, "SegmentURL", media=target)
            if fragment.get("range"):
                entry.set("mediaRange", str(fragment["range"]))
    return ET.tostring(root, encoding="unicode")
