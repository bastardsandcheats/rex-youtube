"""Anonymous TikTok extraction; no account or persistent cookies."""

TEST_URLS = [
    {
        "kind": "video",
        "url": "https://www.tiktok.com/@astrosamantha/video/7166258449645407493",
    },
    {"kind": "collection", "url": "https://www.tiktok.com/@astrosamantha"},
    {"kind": "live", "url": "https://www.tiktok.com/@weathernewslive/live"},
]
NAME = "TikTok"


def prepare(url):
    """Request anonymous Chrome impersonation without loading account cookies."""
    return url, {"impersonate": "chrome"}


def live_url(url):
    """Only a public creator root has a corresponding live endpoint."""
    import re

    match = re.fullmatch(r"(https?://(?:www\.)?tiktok\.com/@[^/]+)(?:/live)?/?", url)
    return match[1] + "/live" if match else None
