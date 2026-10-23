"""Archived VOD collections stay separate from transient live broadcasts."""

import re
import requests
import yt_dlp

TEST_URLS = [
    {"kind": "video", "url": "https://www.twitch.tv/videos/2905866465"},
    {
        "kind": "collection",
        "url": "https://www.twitch.tv/csharpfritz/videos?filter=archives&sort=time",
    },
    {"kind": "live", "url": "https://www.twitch.tv/bashbunni"},
]
NAME = "Twitch"


def resolve(url, *, flat=False):
    """Resolve live channel roots through Streamlink; VOD extraction remains standard yt-dlp."""
    if not flat and re.fullmatch(r"https?://(?:www\.)?twitch.tv/[\w-]+/?", url):
        from app.source import streamlink_live

        return streamlink_live(url)
    return None


def collection_url(url):
    """Select archived VODs explicitly so channel trailers are not indexed as episodes."""
    if re.fullmatch(r"https?://(?:www\.)?twitch.tv/[\w-]+/videos/?", url):
        return url.rstrip("/") + "?filter=archives&sort=time"
    return url


def targets(url):
    """A channel root indexes its archived uploads and checks its separate live broadcast."""
    return (
        [url.rstrip("/") + "/videos", url]
        if re.fullmatch(r"https?://(?:www\.)?twitch.tv/[\w-]+/?", url)
        else [url]
    )


def live_status(info):
    """A recording VOD remains an episode, even when the extractor labels it ongoing."""
    return not str(info.get("extractor_key", "")).lower().startswith(
        "twitchvod"
    ) and bool(info.get("is_live") or info.get("live_status") == "is_live")


def creator(ident):
    """Read public channel biography and artwork using yt-dlp’s anonymous client identity."""
    from yt_dlp.extractor.twitch import TwitchBaseIE

    response = requests.post(
        "https://gql.twitch.tv/gql",
        headers={
            "Client-ID": TwitchBaseIE(
                yt_dlp.YoutubeDL({"quiet": True, "cachedir": False})
            )._CLIENT_ID
        },
        json={
            "query": "query Creator($login:String!){user(login:$login){description profileImageURL(width:300) createdAt}}",
            "variables": {"login": str(ident)},
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json().get("data", {}).get("user") or {}
    return {
        "description": data.get("description"),
        "art": data.get("profileImageURL"),
        "year": (data.get("createdAt") or "")[:4] or None,
    }


def live_url(url):
    """Channel roots are live endpoints; /videos collections point to the same creator."""
    match = re.fullmatch(
        r"(https?://(?:www\.)?twitch\.tv/[\w-]+)(?:/videos(?:\?.*)?)?/?", url
    )
    return match[1] if match else None
