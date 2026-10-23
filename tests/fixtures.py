"""Synthetic extractor output for offline tests; no website requests are made."""


def info(extractor="Youtube", ident="123"):
    return {
        "id": ident,
        "extractor_key": extractor,
        "webpage_url": "https://videos.example/watch/" + ident,
        "title": "Open-source electronics workshop",
        "description": "A public maker lesson.",
        "channel_id": "maker",
        "channel": "Maker Lab",
        "upload_date": "20260102",
        "duration": 120,
        "thumbnail": "https://images.example/thumb.jpg",
        "formats": [
            {
                "url": "https://cdn.example/movie.mp4?signature=transient-fixture",
                "protocol": "https",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "mp4a.40.2",
                "width": 1920,
                "height": 1080,
                "asr": 48000,
            }
        ],
    }
