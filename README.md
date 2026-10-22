# Rex YouTube

An example of feeding custom media into Rex using its feed API and an HTTP server. This source reads YouTube channel URLs from a file, publishes channels and videos as shows and episodes, and streams playback on demand through yt-dlp. No media downloads or disk cache are needed.

## Files

```text
/opt/rex-youtube/
├── service.py                  application
├── requirements.txt
├── config/
│   ├── config.json             active settings
│   ├── config.example.json
│   └── channels.txt            channels to follow
├── data/
│   └── metadata.db             SQLite database
├── logs/                       rotating service log and verification logs
├── tests/                      unit tests and Rex patch regression checks
├── patches/rex-hls-vod.patch   required Rex HLS support
├── deploy/rex-youtube.service  example systemd unit
└── venv/
```

The database preserves channel/video identities, titles, descriptions, dates, episode numbers, format information and Rex keys. Keep it when upgrading. Signed YouTube playback URLs are refreshed in memory and are never stored in the database. Uploaded thumbnails are converted to JPEG in memory and stored by Rex.

## Channels and settings

Edit `config/channels.txt`, one channel URL per line. An optional `| Show name` overrides the channel name:

```text
https://www.youtube.com/@Dropping-Names | Dropping Names
```

`config/config.json` sets the Rex URL, library/source names, relay address and port, polling interval, batch size, maximum height and log level. The default log level is `INFO`; `DEBUG`, `WARNING` and `ERROR` are also available. The service creates or reuses the **YouTube** TV library.

Each channel is a show. Seasons are upload years; episodes run oldest to newest within each year. Upload dates and the channel's listing order determine numbering. Titles, summaries, thumbnails, and channel/season artwork come from YouTube. Older uploads are backfilled, five per 15-minute cycle by default. Members-only or unavailable videos are skipped and retried after 24 hours.

## Playback

The relay selects H.264 video up to 1080p and original audio from YouTube's VOD HLS formats. Duration and codec information come from yt-dlp; indexing does not probe every video. The controlled HLS master lists the audio rendition first, so its declared stream indices are **audio 0, video 1**. Rex maps video before audio when producing DASH for Plex Web.

The source streams playlists and segments through memory. Rex uses its normal temporary transcoder output. Neither a complete video file nor a persistent media cache is created by this service.

Rex needs the included [HLS support patch](patches/rex-hls-vod.patch) for YouTube playback. It allows HLS feed items, serves the correct playlist MIME type, and supports copying H.264/AAC into Plex-compatible MP4/DASH. Apply it to your Rex source and rebuild Rex:

```sh
git -C /path/to/rex-source apply /path/to/rex-youtube/patches/rex-hls-vod.patch
```

The patch was tested against Rex commit `53bfa56b207994817d75e61d8a5d642e898a7a8a`. No other Rex patches are required by this example.

## Setup

Install Python 3.13 and Node.js, then create the environment and configuration:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config/config.example.json config/config.json
cp config/channels.example.txt config/channels.txt
```

Set your Rex address in `config/config.json` and add your channels to `config/channels.txt`. For authenticated Rex access, set the `REX_TOKEN` environment variable or `rex_token` in your private configuration. API requests and artwork uploads use that token through the standard `X-Plex-Token` header. Run `venv/bin/python service.py`, or adapt `deploy/rex-youtube.service` to your installation path and install it with systemd.

## Operations

```sh
systemctl status rex-youtube
systemctl restart rex-youtube
journalctl -u rex-youtube -f
tail -f /opt/rex-youtube/logs/rex-youtube.log
cd /opt/rex-youtube
venv/bin/python -m unittest discover -s tests
```

Restart after editing channels or settings. Dependencies are Python 3.13, Node.js for yt-dlp's YouTube extraction, and `requirements.txt`. The relay listens on port 8790; `/health` reports service availability. It has no web UI, refuses deletion, and does not proxy caller-supplied URLs.

Rex patch regression checks run without editing its upstream checkout:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

Keep active configuration, the database, logs and virtual environments out of version control. The included example configuration contains placeholders for your Rex address.

## Development

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/black --check service.py tests
venv/bin/ruff check service.py tests
venv/bin/python -m unittest discover -s tests
venv/bin/pip-audit -r requirements.txt
```

Tests run with the example configuration when no active configuration is present. `REX_TOKEN` is sent only to Rex, including artwork uploads. The relay exposes playback for indexed videos to hosts that can reach its listener; bind it to the intended network interface.

Before publishing, stage only source code, documentation, example configuration, tests and the HLS patch. Keep active configuration, tokens, databases, logs, diagnostics and virtual environments local.
