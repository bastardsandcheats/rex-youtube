"""Dailymotion extraction is shared; creator details come from its public API."""

from datetime import datetime, timezone
import requests

TEST_URLS = [
    {"kind": "video", "url": "https://www.dailymotion.com/video/x7bs2wf"},
    {"kind": "collection", "url": "https://www.dailymotion.com/ComputerHoy"},
]
NAME = "Dailymotion"


def creator(ident):
    """Read public biography, avatar and account creation year."""
    response = requests.get(
        "https://api.dailymotion.com/user/" + str(ident),
        params={"fields": "description,avatar_720_url,created_time"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "description": data.get("description"),
        "art": data.get("avatar_720_url"),
        "year": (
            datetime.fromtimestamp(data["created_time"], timezone.utc).year
            if data.get("created_time")
            else None
        ),
    }
