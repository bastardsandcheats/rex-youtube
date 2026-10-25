# Agent handoff

Updated 23 October 2026. This file records project decisions and verified results, not a guarantee that external sites still behave the same way. Check the current source and Git status before changing anything.

## Purpose and boundaries

This is **rex-youtube**, a small, public, copy-and-hack example of feeding web video into Rex. It is a separate project from **Scud** (local disk media). Do not put this service's code, settings or database into Scud.

- Extract public video metadata with yt-dlp; use Streamlink where live extraction needs it.
- Publish explicit movie/episode placement, source titles, summaries and artwork through Rex's feed/library APIs.
- Serve playback through our HTTP/HLS/DASH relay. Resolve signed URLs near playback and keep them in RAM.
- Persist only settings and a small metadata catalog. Do not add persistent video downloads or disk media caching. Bounded live buffering is in RAM; separate-track copy-muxing uses FIFO pipes.
- This is an example, not a production compatibility framework. Do not restore old JSON or URL-list formats. The user now requires existing YAML and catalogs to survive additive changes; preserve existing identities and settings defaults.
- Documentation must describe the public example. Do not expose the maintainer's private Rex patches, addresses, credentials or deployment details.

## Read these first

- `README.md`: setup, behavior and playback limits.
- `docs/ARCHITECTURE.md`: runtime map and diagrams.
- `docs/SETTINGS.md` and `config/config.example.yml`: commented YAML settings.
- `docs/ADDING_A_PROVIDER.md` and `docs/PROVIDER_CONTRACT.md`: adding a site and required output shapes.
- `docs/CHECKING_PROVIDERS.md`: CLI/API checks, stages and result levels.
- `docs/TEST_FINDINGS.md`: test evidence, failures and tested Rex revisions.

## Source layout

| Path | Responsibility |
| --- | --- |
| `service.py` | Small launcher; implementation belongs in `app/` |
| `app/config.py` | YAML validation, defaults and per-source inheritance |
| `app/provider_registry.py` | Automatic discovery and validation of site modules |
| `app/source.py` | Shared extraction, normalization and source options |
| `app/playback.py` | Format selection, declared tracks and manifest rewriting |
| `app/live.py` | Bounded live buffering, recovery and copy-muxing |
| `app/service.py` | SQLite catalog, Rex publishing, HTTP relay and polling |
| `app/checks.py` | Isolated URL checks using temporary state, without Rex |
| `app/reporting.py` | Generated inventories and Markdown/JSON reports |
| `providers/*.py` | Site definitions, test URLs and optional site-specific hooks |
| `scripts/check_providers.py` | Arbitrary URL, provider-suite and configured-source checks |
| `scripts/probe_sites.py` | Optional integration checks against a disposable Rex |
| `scripts/probe_live_buffer.py` | Synthetic interruption/buffering check |
| `tests/` | Offline regression tests; shared mock data in `fixtures.py` |

## Adding providers

Every non-dunder Python file in `providers/` is discovered as a provider. Keep framework/registry/helper modules outside that folder. A standard yt-dlp provider normally needs only a module description, `NAME` and `TEST_URLS` near the top. Add documented optional hooks only when needed; see the provider contract for signatures.

YouTube individual videos use standard yt-dlp extraction. Its custom functions choose channel uploads/live URLs and expand flat entries; it has no separate decoder. Twitch live uses a Streamlink override. Vimeo adds public embed/oEmbed and creator helpers. Do not duplicate shared playback code per site.

Keep real test URLs in each provider's `TEST_URLS`; do not scatter duplicate lists across tests/scripts. Shared offline tests remain useful independently of provider network checks. `test_source_contract.py` tests shared normalization, not another list of sites.

A URL can be checked without registering a provider or editing active settings. Restart the service after adding a provider module.

## Settings and metadata decisions

- Active settings: `config/config.yml`; public template: `config/config.example.yml`.
- Sections: `server`, `rex`, `scan`, `playback`, `defaults`, `sites`.
- Each site can override show/movie/live library names and contain multiple source entries.
- Source options include `url`, `name`, `type`, `year_seasons`, `season`, `max_items`, `batch_size`, `max_height`, `check_live`, `enabled`.
- `type: auto` is the default: omit redundant declarations.
- Channel collections normally become shows; individual items become movies. Publication years become seasons by default. New placements are allocated in upload-date order; existing episode numbers never move when older uploads are backfilled.
- Live streams normally share **Live Streams**, with per-site overrides available. Twitch VODs remain channel shows, separate from transient live items.
- `max_items` limits discovery, not the cached snapshot. Source removal and successful uncapped listings reconcile ownership; failures never imply deletion. Batch size also bounds periodic metadata refresh.
- Preserve source-provided metadata. Report missing metadata instead of inventing it. Do not erase an existing summary with an empty refresh.
- The service reads YAML without rewriting it, preserving comments. Unknown and duplicate keys are rejected.
- Disable/remove a source before deleting its library, or polling can recreate it. Library deletion affects catalog state, not upstream videos.

## Verification commands

Use Python 3.13, Node.js, FFmpeg/ffprobe and the pinned requirements. Install development tools from `requirements-dev.txt` when needed.

```sh
venv/bin/python -m unittest discover -s tests -q
venv/bin/black --check app providers scripts tests service.py
venv/bin/ruff check app providers scripts tests service.py
git diff --check

# Public URL; no private configuration or Rex required:
venv/bin/python scripts/check_providers.py 'https://example.org/public-video'
venv/bin/python scripts/check_providers.py --provider youtube
venv/bin/python scripts/check_providers.py --all
venv/bin/python scripts/check_providers.py --configured

# Optional integration test: starts a temporary catalog, not production libraries.
venv/bin/python scripts/probe_sites.py --site youtube --rex-bin /path/to/rex
```

Provider checks separately report extraction, metadata completeness, artwork, formats, relay access, declared/observed tracks, decoded playback and seeking. Partial metadata or failed seeking must not be presented as full support. Offline/live-unavailable is distinct from broken playback. A collection check samples a representative item, not every upload. Network failures may be external restrictions; retain the actual failure boundary.

`POST /api/providers/check` runs an isolated check; `GET /api/providers` lists definitions. Loopback checks are allowed; remote checks require the configured Rex token. Workers have deadlines and do not publish to Rex or modify the active catalog.

Reports replace canonical files rather than accumulating diagnostics. `reports/PROVIDERS.md` is generated from definitions: change the generator and regenerate it instead of hand-editing it. Dynamic `latest`, `suite` and `configured` Markdown/JSON reports remain local. Keep diagrams narrow, with short wrapped labels; long horizontal pipelines are unreadable on GitHub.

## Verified compatibility baseline

Earlier fast playback baseline: Rex **`306f4aa992700f6a3df5eabd5eca731d24dbedf5`**, 23 October 2026, **without the manifest MIME patch**. The local updater rebuilt/deployed that revision and its health check passed. Playback probes used the same built executable with disposable catalogs.

- YouTube HLS and Dailymotion HLS: short audio/video decode through Rex DASH, direct Part decode and short seek requests passed.
- Separate-track DASH: the same conversion/direct Part/seek checks passed. The direct response still advertised `video/mp4`; FFmpeg handled it, which does not prove every native client will.
- TikTok progressive H.264/AAC: short playback through Rex DASH passed.
- Upstream feed checks: 8/8 rejection checks and 17/17 display checks passed.
- Project offline suite: 80 tests passed.

The former MIME patch was removed in `c5d0e16`. Do not restore it merely because older docs/demo results mention it. Upstream Rex now redirects direct HLS Part reads through conversion (`fe4af5e`) and accepts `.m3u8` feed labels with an HLS probe (`de344f7`). This example still uses logical `.mp4` feed labels with actual manifest paths. The optional `tests/rex/Dockerfile` now tests upstream without applying a patch; that revised Docker test recipe was not rebuilt during the fast checks.

The demo was recorded on older Rex `3bdf52bcb2e13ad414b06133768b15d6a753b1fc` with the former patch. The older full native-transcode baseline was 247/250 on `a9d5eedc7a7fe39e8b0b1693547d3d9b1300b0c4`; failures concerned far-seek timing, a Vorbis tone assertion and HDR color tags. Do not describe those older results as newly retested.

## Known limits

Six providers have successful recorded paths: YouTube, Vimeo, Dailymotion, PeerTube, TikTok and Twitch. Bilibili anonymous extraction was blocked/unverified.

Plex Web live playback remains unresolved in the recorded tests, despite native live decoding. Vimeo far seeking, some Dailymotion uploads and some TikTok HEVC uploads have partial/failed paths. Authentication-only, subscriber-only, private and DRM media are unsupported. Full client walkthroughs, indefinite live playback and the full upstream playback baseline were not repeated against the latest revision. See test findings before making support claims.

## Workspace and publishing hygiene

Keep settings, tokens, databases/WAL files, logs, virtual environments, browser state and diagnostics out of Git. The maintainer explicitly wants `.gitignore` itself ignored too; preserve that choice and never force-add it. Only the example YAML should be public. Inspect the staged file list before committing; avoid dumping credentials or signed URLs into command output or reports.

Local runtime state belongs under this project's directory: `config/`, `data/metadata.db`, rotating `logs/` and `venv/`. Logs default to INFO. Temporary media fixtures are for tests, not a service cache. Summarize meaningful diagnostic findings in `docs/TEST_FINDINGS.md` and clean disposable artifacts.

The public repository is `https://github.com/bastardsandcheats/rex-youtube`. Publishing a code/docs push and creating a GitHub release are separate actions; follow the current request. Draft community messages without sending them unless explicitly asked to send.

On the maintainer's host, the service unit is `rex-youtube`; Rex is maintained separately with its own `update.sh`. Do not run an updater or alter unrelated services merely to inspect compatibility. If `gh` reports no login in a launcher without HOME, check its existing configuration location before requesting new authentication; never print its token.

## Addon-guide alignment and collections (23 October 2026)

Use feed `display` for source metadata; do not lock source titles through the library edit API. `probe` is true (whole bounded ffprobe response), false (no indexing probe), or headers (prior lightweight default). Refresh metadata in bounded batches (`refresh_hours`, default 24). Complete cached snapshots run every six hours (`scan.snapshot_hours`) only after successful scans/delivery. Do not force shrink guards. Changed delivery selection gets a new feed ID and file label; refresh requests pin format IDs/layout.

Collections are independent of show/movie presentation: `collection: NAME|true|false`, `type: collection`, `collection_type: movie|show`, and `live_collection: NAME|true|false` (true by default). Follow `app/collections.py` ownership/GUID checks and PlexControl's verified member APIs. Do not modify another app's collections. The current focused suite has 95 passing tests. New API integration and one full-probed YouTube playback were verified on Rex `9671faa2dda20abff12a37885ab2dc0b4bcffccb`; older playback evidence above remains historical.

## Incremental YouTube discovery (25 October 2026)

`app/discovery.py` separates durable discovered IDs from metadata jobs. YouTube
channel polls use `source.iter_youtube_uploads`, raw lazy newest-first entries,
three known IDs of overlap, and a persisted Jan-1 boundary from the year added.
Exact dates gate publication; weekly year-window reconciliation never treats
unseen older media as deletions. Existing older items are retained unless the
user explicitly asks to remove them. New jobs are uncapped by legacy extraction
limits; historical metadata uses `scan.backfill_batch_size`. Restricted uploads
are tracked without busy retries. `/api/discovery` reports counts. Playback
layout changes explicitly queue refresh and wake the scanner. Connections are
thread-local; publication is event-driven; idle scans must not do collection or
HTTP work. Test paging with a generator that fails if an extra page is read.
