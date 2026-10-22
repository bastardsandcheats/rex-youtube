# Rex YouTube

A small example of feeding public web video into Rex using its feed API and an HTTP server. Despite the historical name, `rex-youtube` includes YouTube, Vimeo, Dailymotion, PeerTube, TikTok and Twitch examples. yt-dlp does the extraction; Streamlink handles public Twitch live streams. This is an educational source adapter, not a universal downloader.

```text
sources.txt → yt-dlp / Streamlink → normalized metadata → Rex feed/library API
playback request → fresh URL resolution → HTTP / HLS / DASH relay → Rex → Plex
```

## Sources and libraries

Put one public URL per line in `config/sources.txt`. Blank lines and comments are ignored. An optional `| Show name` overrides a collection's name. Existing installations using `config/channels.txt` continue to work when `sources.txt` is absent.

```text
https://www.youtube.com/@TechnologyConnections
https://vimeo.com/1084537
https://www.dailymotion.com/video/x7bs2wf
```

The extractor determines the site. Collections become shows in a library named after the provider (for example **YouTube** or **PeerTube**). The creator is the show, publication year is the season, and episodes run oldest to newest within each year. Missing publication dates use season 0. Single-video URLs become movies in **YouTube Videos**, **Vimeo Videos**, etc. Titles, descriptions and artwork come from the source, rather than a movie database. The optional `library` setting retains the legacy YouTube show-library override.

A Twitch channel root discovers its VOD collection and, when online, a separate temporary movie in **Twitch Live**. `/videos` selects just the VOD collection. Confirmed offline results remove only that live item; network errors preserve it. VODs remain persistent. There is no EPG, DVR or live-TV library.

New collections backfill five entries per 15-minute poll by default, up to the configured `collection_limit` (default 1000). Unavailable videos are skipped and retried after 24 hours. One failed source does not stop other sources.

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

Tested against upstream Rex **`71570e16f2a74631ee90891f47942b067cf5a93f`**. The previous HLS patch is no longer required or included. Use a current Rex build with native DASH playback support.

The relay chooses compatible H.264 video up to `max_height` (1080 by default) and available audio. Indexing uses extractor duration/codec information. Separate-track manifests have a controlled track order and need no probe. Muxed HLS does not declare its track indices, so those items get a small ffprobe header check before publishing; guessing the order breaks browser playback. Progressive files pass through with HTTP Range support. HLS playlists are rewritten to relay segments and anonymous request headers. DASH and separate audio/video formats are exposed as a small MPD. Rex/FFmpeg performs normal playback conversion.

Rex's feed currently accepts video file labels, rather than `.m3u8`/`.mpd` file labels. The feed therefore uses a logical `.mp4` **file** label while **path** points to the actual manifest. This does not create an MP4 or invent a file size. Plex Web uses Rex's DASH transcoder for these manifest sources; clients that insist on direct-playing the logical MP4 may fail.

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

For live playback only, a small FFmpeg process copies the selected HLS streams into MPEG-TS so Rex's Part endpoint serves actual video bytes. It does **not** encode or save the broadcast. Continuous playback through that endpoint was verified for 12 seconds; indefinite playback, Plex Web's unknown-duration behavior and recovery from broadcaster discontinuities are not guaranteed. A client disconnect stops the muxer; reopening resolves a fresh stream. An ended broadcast disappears on the next successful offline poll.

## Verification

Real media-path checks use public technology/science content and official Blender samples. They resolve a source, ffprobe its relay, feed an isolated temporary Rex catalog and decode video **and audio** through Rex. They never modify a user's libraries. External sites can change or restrict access by IP.

| Source | Backend | Verified on this host |
| --- | --- | --- |
| YouTube | yt-dlp | HLS relay and Rex DASH decode |
| Vimeo | yt-dlp public embed player | Clear HLS relay and Rex DASH decode |
| Dailymotion | yt-dlp | HLS relay and Rex DASH decode |
| PeerTube | yt-dlp | HLS relay and Rex DASH decode |
| TikTok | yt-dlp, anonymous browser impersonation | Progressive relay and Rex DASH decode |
| Twitch VOD | yt-dlp | HLS relay and Rex DASH decode |
| Twitch live | Streamlink | Continuous relay and Rex Part decode |
| Bilibili | yt-dlp, anonymous browser impersonation | Experimental: public pages/API blocked by anti-bot responses here; playback unverified |
| Public DASH sample | yt-dlp generic extractor | Separate-track DASH relay and Rex DASH decode |

Run the offline suite and optional network probes:

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/black --check service.py providers.py playback.py scripts tests
venv/bin/ruff check service.py providers.py playback.py scripts tests
venv/bin/python -m unittest discover -s tests
venv/bin/pip-audit -r requirements.txt
venv/bin/python scripts/probe_sites.py --site youtube --site dash-demo --rex-bin /path/to/rex-server
# An optional currently live public programming stream:
venv/bin/python scripts/probe_sites.py --live-url https://www.twitch.tv/CHANNEL --rex-bin /path/to/rex-server
```

The upstream `tests/test_native_transcode.py` suite was also run against this Rex build and FFmpeg 7.1.5: **247/250 checks passed**. The failures were a far-seek timestamp expectation, a Vorbis tone check and an HDR conversion color-tag check. These are upstream playback limits to investigate, not a claim that every format works. To repeat it, follow Rex's native test instructions and set `REX_SERVER` and `REX_TRANSCODER` to your binaries. A clean upstream Rust playback unit-test build is also available:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

## Reading and extending the example

`providers.py` extracts and normalizes public metadata; `playback.py` selects formats and creates manifests; `service.py` maintains the small SQLite catalog, publishes metadata/artwork and serves HTTP. `scripts/probe_sites.py` is the opt-in network check. There is no web UI.

Most yt-dlp extractors already follow the shared path: add a provider label if desired, put a public URL in the source list and prove its playback with the probe script. Add a small special case only when the extractor's metadata or transport actually differs. Vimeo's anonymous embed and Twitch's live resolver are the current examples.

The relay serves indexed media to hosts that can reach it, refuses client deletion and accepts no caller-supplied upstream URL. Bind it to the intended network. Source URLs and artwork are trusted administrator configuration. Do not expose private settings or diagnostic output when sharing the project.
