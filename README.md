# Rex YouTube

A small example of feeding public web video into Rex using its feed API and an HTTP server. Despite the historical name, `rex-youtube` includes YouTube, Vimeo, Dailymotion, PeerTube, TikTok and Twitch examples. yt-dlp does the extraction; Streamlink handles public Twitch live streams. This is an educational source adapter, not a universal downloader.

```text
sources.txt → yt-dlp / Streamlink → normalized metadata → Rex feed/library API
playback request → fresh URL resolution → HTTP / HLS / DASH relay → Rex → Plex
```

[Watch the demo](demo/rex-youtube-demo.mp4): six channel libraries with actual Plex Web playback, plus the shared Live Streams library and three native Rex live-playback clips. [Demo details](demo/README.md).

## Sources and libraries

Put one public URL per line in `config/sources.txt`. Blank lines and comments are ignored. An optional `| Show name` overrides a collection's name. Existing installations using `config/channels.txt` continue to work when `sources.txt` is absent.

```text
https://www.youtube.com/@TechnologyConnections/videos
https://vimeo.com/businessofsoftware
https://www.dailymotion.com/ComputerHoy
https://framatube.org/c/joinpeertube/videos
https://www.tiktok.com/@astrosamantha
https://www.twitch.tv/csharpfritz/videos?filter=archives&sort=time
```

The extractor determines the site. Collections become shows in a library named after the provider (for example **YouTube** or **PeerTube**). The creator is the show, publication year is the season, and episodes run oldest to newest within each year. Missing publication dates use season 0. Single-video URLs become movies in **YouTube Videos**, **Vimeo Videos**, etc. Titles, descriptions and artwork come from the source, rather than a movie database. Creator artwork is preferred for show/season posters; when no avatar is exposed, an episode thumbnail is used. Vimeo’s public oEmbed fills upload dates missing from its anonymous player. The optional `library` setting retains the legacy YouTube show-library override.

A Twitch channel root discovers its VOD collection and, when online, a separate temporary movie in **Live Streams**. `/videos` selects just the VOD collection. Confirmed offline results remove only that live item; network errors preserve it. VODs remain persistent in the **Twitch** show library, grouped by channel. TikTok `/@creator/live` URLs and other yt-dlp live sources use the same **Live Streams** movie library. Streamlink is also tried for live URLs with a matching plugin when yt-dlp has no dedicated extractor; it is not a separate library. There is no EPG, DVR or live-TV library.

New collections backfill five entries per 15-minute poll by default, up to the configured `collection_limit` (default 1000). Unavailable videos are skipped and retried after 24 hours. One failed source does not stop other sources.

The [expanded source list](config/sources.example.txt) includes multiple creators per site: Computerphile and Raspberry Pi; Linux Foundation; TechRadar and Clubic; Blender Open Movies; NASA; and the bashbunni and anthonywritescode Twitch archives. It also includes ISS, aquarium, nature and TikTok weather live streams. These are public examples, not a promise that any particular broadcast or upload will remain available. Some Vimeo uploads reject anonymous access even when their channel lists them.

Episode descriptions, publication dates and thumbnails are imported when exposed by the provider. Public channel bios and avatars are also used where available. Missing descriptions are left empty rather than generated, and an empty update does not erase an existing summary.

## Setup

Install Python 3.13, Node.js (YouTube extraction) and FFmpeg/ffprobe (HLS layout checks and live copy-muxing), then:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config/config.example.json config/config.json
cp config/sources.example.txt config/sources.txt
```

Set `rex_url` in `config/config.json` to your Rex address. For authenticated Rex access, use `REX_TOKEN` or `rex_token` in that private file. This token goes only to Rex. Configure the relay's listening `host` and `port`; `source_url` can override the advertised HTTP address when automatic interface detection is unsuitable. `source` is the stable Rex feed identifier: retain it when upgrading.

Run `venv/bin/python service.py`, or adapt `deploy/rex-youtube.service` to your installation directory. Restart after editing settings or sources. `/health` reports availability. Logs default to `INFO`; `DEBUG`, `WARNING` and `ERROR` are available.

```sh
systemctl restart rex-youtube
journalctl -u rex-youtube -f
tail -f /opt/rex-youtube/logs/rex-youtube.log
```

Keep `data/metadata.db` when upgrading: it holds identities, canonical URLs, metadata, episode ordering and Rex keys. Existing YouTube rows migrate without changing feed IDs or Rex keys. Active configuration, tokens, the database, logs, diagnostics and virtual environments belong outside version control.

## Playback and Rex compatibility

The current browser walkthrough was recorded against upstream Rex **`3bdf52bcb2e13ad414b06133768b15d6a753b1fc`** with the included [manifest MIME patch](patches/rex-manifest-mime.patch). Plex Web uses Rex’s native DASH playback support. Native clients reading the Part endpoint need this patch so HLS/DASH responses retain their upstream content type instead of being labelled MP4. The older HLS playback patch is not required.

The relay chooses compatible H.264 video up to `max_height` (1080 by default) and available audio. Indexing uses extractor duration/codec information. Separate-track manifests have a controlled track order and need no probe. Muxed HLS does not declare its track indices, so those items get a small ffprobe header check before publishing; guessing the order breaks browser playback. Progressive files pass through with HTTP Range support. HLS playlists are rewritten to relay segments and anonymous request headers. DASH and separate audio/video formats are exposed as a small MPD. Rex/FFmpeg performs normal playback conversion.

Rex's feed currently accepts video file labels, rather than `.m3u8`/`.mpd` file labels. The feed therefore uses a logical `.mp4` **file** label while **path** points to the actual manifest. This does not create an MP4 or invent a file size. Plex Web uses Rex's DASH transcoder for these manifest sources; the included MIME patch lets native clients identify the manifest behind that logical label.

| Media | Behavior and limits |
| --- | --- |
| H.264 + AAC, progressive MP4 | Simplest compatible path; Range requests pass through. |
| Clear VOD HLS, MPEG-TS segments | Relayed without local encoding; Rex can copy compatible video/audio into DASH. |
| Clear DASH, separate audio/video | Relayed through an MPD; Rex combines the selected tracks. |
| HEVC, VP9/AV1, AC-3, DTS; MKV/WebM | Rex has client-dependent remux/transcode paths. Requires its FFmpeg build and suitable client capabilities; not every codec/client combination is verified by this example. |
| Subtitles, HDR | Rex has dedicated handling/tests, but this source does not import external subtitle tracks or promise HDR tone mapping. |
| DRM, account/private/subscriber content | Unsupported. No login, OAuth, persistent cookies or user-account flows. |
| Live streams | Experimental continuous MPEG-TS playback; no finite duration, seek history or DVR. See below. |

Signed playback URLs, anonymous cookies and extractor headers live only in RAM and are refreshed close to playback (120-second resolution cache). No complete media file or persistent media cache is created. Rex still uses its normal temporary transcoder output. Artwork is converted to JPEG in memory and stored by Rex.

Live playback uses a bounded in-memory Streamlink buffer (16 MiB per viewer by default, configurable with `live_buffer_mb`) and FFmpeg copies the stream into MPEG-TS without encoding or saving it. Up to four relays can run concurrently. Temporary interruptions trigger signed-URL refresh and reconnection while the HTTP response stays open; MPEG-TS null packets keep an idle connection alive without inventing video frames. A client disconnect stops the relay. Confirmed offline broadcasts disappear on the next successful poll.

The recovery probe delivered all 60 video frames across an injected 18-second outage with a 15-second HTTP read timeout. Real Twitch live playback decoded for 60 seconds, and TikTok and NASA ISS live for 12 seconds. Aquarium and nature streams also passed 12-second native checks. Separate YouTube live video/audio renditions are copy-muxed through FIFO pipes and share the read-ahead budget. This provides a bounded recovery window, not indefinite playback or DVR. Plex Web did not advance on the tested TikTok live item; browser live playback remains experimental.

## Verification

Real media-path checks use public technology/science content and official Blender samples. They resolve a source, ffprobe its relay, feed an isolated temporary Rex catalog and decode video **and audio** through Rex. They never modify a user's libraries. External sites can change or restrict access by IP.

| Source | Backend | Verified on this host |
| --- | --- | --- |
| YouTube | yt-dlp | HLS relay and Rex DASH decode |
| Vimeo | yt-dlp public embed player | Clear HLS relay and Rex DASH decode |
| Dailymotion | yt-dlp | Clubic Plex Web playback and native Rex decode; one TechRadar upload stalled in the browser |
| PeerTube | yt-dlp | HLS relay and Rex DASH decode |
| TikTok | yt-dlp, anonymous browser impersonation | H.264 progressive relay, Rex DASH decode and NASA Plex Web playback; some HEVC uploads stalled |
| Twitch VOD | yt-dlp | HLS relay and Rex DASH decode |
| Twitch live | Streamlink | Continuous relay and Rex Part decode |
| TikTok live | yt-dlp | Continuous relay and Rex Part decode; Plex Web stalled |
| YouTube live | yt-dlp + Streamlink buffer | ISS, aquarium and nature native Rex playback; Plex Web stalled |
| Bilibili | yt-dlp, anonymous browser impersonation | Experimental: public pages/API blocked by anti-bot responses here; playback unverified |
| Public DASH sample | yt-dlp generic extractor | Separate-track DASH relay and Rex DASH decode |

With the MIME patch, native Rex Part playback also decoded 12 minutes of a Twitch VOD and complete Dailymotion videos of 271 and 143 seconds. These checks exercise the native media path; they do not establish that every Plezy client works.

Channel collections from all six accessible providers were also fed as **episodes** and decoded through Rex. The walkthrough uses these creator/show libraries rather than just single-video movie libraries.

The offline suite has 45 passing checks. Run it and the optional network probes:

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/black --check service.py providers.py playback.py live.py scripts tests
venv/bin/ruff check service.py providers.py playback.py live.py scripts tests
venv/bin/python -m unittest discover -s tests
venv/bin/python scripts/probe_live_buffer.py
venv/bin/pip-audit -r requirements.txt
venv/bin/python scripts/probe_sites.py --site youtube --site dash-demo --rex-bin /path/to/rex-server
venv/bin/python scripts/probe_sites.py --collections --rex-bin /path/to/rex-server
# Optional currently live public programming/weather streams:
venv/bin/python scripts/probe_sites.py --site live-only --live-url https://www.twitch.tv/CHANNEL --rex-bin /path/to/rex-server
venv/bin/python scripts/probe_sites.py --site live-only --live-url https://www.tiktok.com/@weathernewslive/live --rex-bin /path/to/rex-server
```

Continuous Vimeo playback also passed in Plex Web, but a far seek into its HLS stream stalled.

The upstream `tests/test_native_transcode.py` baseline was run against Rex `a9d5eedc7a7fe39e8b0b1693547d3d9b1300b0c4` with the MIME patch and FFmpeg 7.1.5 (before the later browser walkthrough build): **247/250 checks passed**. The failures were a far-seek timestamp expectation, a Vorbis tone check and an HDR conversion color-tag check. These are upstream playback limits to investigate, not a claim that every format works. To repeat it, follow Rex's native test instructions and set `REX_SERVER` and `REX_TRANSCODER` to your binaries. A clean upstream Rust playback unit-test build with only the included manifest MIME patch is also available:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

## Reading and extending the example

`live.py` handles bounded memory buffering and live reconnection; `providers.py` extracts and normalizes public metadata; `playback.py` selects formats and creates manifests; `service.py` maintains the small SQLite catalog, publishes metadata/artwork and serves HTTP. `scripts/probe_sites.py` is the opt-in network check. There is no web UI.

Most yt-dlp extractors already follow the shared path: add a provider label if desired, put a public URL in the source list and prove its playback with the probe script. Add a small special case only when the extractor's metadata or transport actually differs. Vimeo's anonymous embed and Twitch's live resolver are the current examples.

To remove a provider library, first remove its URLs from the source list and restart this service, then use Rex’s existing `DELETE /library/sections/{id}` endpoint with its usual authentication. This deletes the catalog library, not upstream videos. Leaving its URLs configured lets the next poll recreate it.

The relay serves indexed media to hosts that can reach it, refuses client media deletion and accepts no caller-supplied upstream URL. Bind it to the intended network. Source URLs and artwork are trusted administrator configuration. Do not expose private settings or diagnostic output when sharing the project.
