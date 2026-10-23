"""Public Vimeo embed extraction and optional anonymous creator metadata."""

import logging
import re
import requests

TEST_URLS = [
    {"kind": "video", "url": "https://vimeo.com/1084537"},
    {"kind": "collection", "url": "https://vimeo.com/businessofsoftware"},
]
NAME = "Vimeo"


def prepare(url):
    """Use the anonymous embed player for numeric video URLs, preserving the public Referer."""
    match = re.fullmatch(r"https?://(?:www\.)?vimeo.com/(\d+)/?", url)
    return (
        (
            "https://player.vimeo.com/video/" + match[1],
            {"http_headers": {"Referer": url}},
        )
        if match
        else (url, {})
    )


def enrich(info, *, flat=False):
    """Fill missing upload dates and creator identity from public oEmbed metadata."""
    if flat or info.get("upload_date"):
        return
    try:
        response = requests.get(
            "https://vimeo.com/api/oembed.json",
            params={"url": "https://vimeo.com/" + str(info["id"])},
            timeout=15,
        )
        response.raise_for_status()
        public = response.json()
        info["upload_date"] = (public.get("upload_date") or "")[:10].replace("-", "")
        info["uploader_url"] = public.get("author_url") or info.get("uploader_url")
        info["uploader_id"] = (info.get("uploader_url") or "").rstrip("/").rsplit(
            "/", 1
        )[-1] or info.get("uploader_id")
    except (requests.RequestException, ValueError):
        logging.getLogger("providers").debug(
            "Vimeo public metadata unavailable; keeping embed metadata"
        )


def creator(ident):
    """Read a public creator biography, avatar and account year."""
    response = requests.get(
        "https://vimeo.com/api/v2/" + str(ident) + "/info.json", timeout=15
    )
    response.raise_for_status()
    data = response.json()
    return {
        "description": data.get("bio"),
        "art": data.get("portrait_huge"),
        "year": (data.get("created_on") or "")[:4] or None,
    }
