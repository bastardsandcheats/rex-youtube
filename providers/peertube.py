"""PeerTube uses standard yt-dlp output across independent instances."""

TEST_URLS = [
    {
        "kind": "video",
        "url": "https://framatube.org/videos/watch/9c9de5e8-0a1e-484a-b099-e80766180a6d",
    },
    {"kind": "collection", "url": "https://framatube.org/c/joinpeertube/videos"},
]
NAME = "PeerTube"


def prepare(url):
    """Accept the long video-channels URL spelling as the extractor’s /c/ channel form."""
    return url.replace("/video-channels/", "/c/"), {}
