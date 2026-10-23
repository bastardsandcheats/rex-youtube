"""Public extraction remains experimental where the site blocks anonymous access."""

TEST_URLS = [
    {
        "kind": "video",
        "url": "https://www.bilibili.com/video/BV15z4y1F7tK/",
        "experimental": True,
    },
]
NAME = "Bilibili"


def prepare(url):
    """Request anonymous Chrome impersonation; this does not bypass account requirements."""
    return url, {"impersonate": "chrome"}
