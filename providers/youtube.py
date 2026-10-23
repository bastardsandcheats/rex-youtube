"""YouTube uses standard yt-dlp extraction, metadata and playback.

Its URL helpers choose uploads/live URLs and expand ID-only flat entries;
individual videos need no YouTube-specific extraction or relay implementation."""

import re

TEST_URLS = [
    {"kind": "video", "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"},
    {
        "kind": "collection",
        "url": "https://www.youtube.com/@TechnologyConnections/videos",
    },
    {"kind": "live", "url": "https://www.youtube.com/@NASA/live"},
]
NAME = "YouTube"


def collection_url(url):
    """Select the uploads collection for channel roots; individual and live URLs stay unchanged."""
    if "/@" in url and not re.search(r"/(videos|streams|shorts|live)/?$", url):
        return url.rstrip("/") + "/videos"
    return url


def live_url(url):
    """A public @channel can expose a separate /live endpoint; watch URLs cannot."""
    match = re.fullmatch(
        r"(https?://(?:www\.)?youtube\.com/@[^/]+)(?:/(?:videos|streams|shorts|live))?/?",
        url,
    )
    return match[1] + "/live" if match else None


def entry_url(entry):
    """Convert an ID-only flat entry to its canonical YouTube watch URL."""
    return "https://www.youtube.com/watch?v=" + str(entry["id"])
