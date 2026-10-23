# Rex YouTube

A small example of feeding public web video into Rex using its feed API and an HTTP server. Despite the historical name, `rex-youtube` includes YouTube, Vimeo, Dailymotion, PeerTube, TikTok and Twitch examples. yt-dlp does the extraction; Streamlink handles public Twitch live streams. This is an educational source adapter, not a universal downloader.

```text
config.yml → yt-dlp / Streamlink → normalized metadata → Rex feed/library API
playback request → fresh URL resolution → HTTP / HLS / DASH relay → Rex → Plex
```

[Watch the demo](demo/rex-youtube-demo.mp4): six channel libraries with actual Plex Web playback, plus the shared Live Streams library and three native Rex live-playback clips. [Demo details](demo/README.md).

## Setup and settings

Install Python 3.13, Node.js and FFmpeg/ffprobe, then:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config/config.example.yml config/config.yml
```

Edit the private YAML: set `rex.url`, the relay listener and the sites/channels/items you want. `rex.token` or the `REX_TOKEN` environment variable supplies a credential when needed. Start with `venv/bin/python service.py`, or adapt `deploy/rex-youtube.service` to your installation directory. Restart after editing settings or adding a provider file.

The [commented YAML example](config/config.example.yml) and [settings guide](docs/SETTINGS.md) cover per-site show/movie/live library names, source name/type overrides, year-based or fixed seasons, item/batch limits, quality and optional channel live checks. `type` defaults to `auto`, so omit it unless you want an override. Comments are preserved because the service does not rewrite settings.

Collections normally become creator shows, publication years become seasons and uploads are ordered oldest-to-newest. Single videos become movies. Live sources share **Live Streams** unless you override a site's live library. Titles, summaries and artwork come from the source. Public creator bios/avatars and missing Vimeo dates are filled when available; missing descriptions are not invented.

```sh
systemctl restart rex-youtube
journalctl -u rex-youtube -f
```

Logs default to INFO and rotate automatically. `/health` reports availability. Private settings, the small `data/metadata.db`, logs and virtual environments stay out of Git. The service keeps no persistent video cache. This is a copy-and-hack example with one YAML settings format and one current metadata schema.

## Check a URL or all providers

```sh
# No registration, settings change or Rex needed to try a public URL.
venv/bin/python scripts/check_providers.py 'https://videos.example/watch/123'

# Discover test cases from every provider file.
venv/bin/python scripts/check_providers.py --all

# Test your actual enabled YAML entries and overrides.
venv/bin/python scripts/check_providers.py --configured
```

Replace the placeholder URL with a real public URL. The checker independently reports extraction, metadata completeness, artwork, real relay access, declared/observed tracks, decoded playback and finite seeking. A partly working source gets a partial result with its individual failures, rather than a blanket “supported”. It never invokes Rex or publishes test items.

Reports are generated automatically in `reports/`: Markdown and JSON for the latest run, complete provider suite and configured-source run. The [generated provider inventory](reports/PROVIDERS.md) includes test URLs, custom handlers and their diagram. Live/experimental fixtures are explicit optional cases. See [checking and report levels](docs/CHECKING_PROVIDERS.md), [architecture diagrams](docs/ARCHITECTURE.md), [adding a provider](docs/ADDING_A_PROVIDER.md) and the [output contract](docs/PROVIDER_CONTRACT.md).

You can also ask the running service with `POST /api/providers/check` and list provider definitions with `GET /api/providers`. The checking guide shows the request and access rules.

## Playback and Rex compatibility

The current browser walkthrough was recorded against upstream Rex **`3bdf52bcb2e13ad414b06133768b15d6a753b1fc`** with the included [manifest MIME patch](patches/rex-manifest-mime.patch). Plex Web uses Rex’s native DASH playback support. Native clients reading the Part endpoint need this patch so HLS/DASH responses retain their upstream content type instead of being labelled MP4.

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

See [consolidated test findings](docs/TEST_FINDINGS.md) for the verified results, fixes, failed checks and remaining limitations.

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

The offline suite covers provider discovery/contracts, YAML settings, worker isolation, HTTP relaying, decoding and report generation. Run it and the optional network probes:

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/black --check service.py app providers scripts tests
venv/bin/ruff check service.py app providers scripts tests
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

Follow the [guide to adding a yt-dlp provider](docs/ADDING_A_PROVIDER.md) for the URL-only path, optional provider naming and playback verification.

`service.py` launches the service. Shared implementation lives in `app/`; each file in `providers/` contains that site's test URLs and optional handlers. [ARCHITECTURE.md](docs/ARCHITECTURE.md) maps the files and request paths. Normal provider checks run without Rex; `scripts/probe_sites.py --rex-bin …` retains optional disposable-Rex integration checks.

A standard yt-dlp site needs only a provider file declaring its name and test URLs. Add custom handlers only when its extraction, URL selection or metadata differs; the generated inventory documents every such handler.

To remove a provider library, first remove or disable its YAML sources and restart this service, then use Rex’s existing `DELETE /library/sections/{id}` endpoint with its usual authentication. This deletes the catalog library, not upstream videos. Leaving its sources enabled lets the next poll recreate it.

The relay serves indexed media to hosts that can reach it, refuses client media deletion and accepts no caller-supplied upstream URL. Bind it to the intended network. Source URLs and artwork are trusted administrator configuration. Do not expose private settings or diagnostic output when sharing the project.
