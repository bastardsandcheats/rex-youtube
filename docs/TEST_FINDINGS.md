# Playback and diagnostic findings

Consolidated on 23 October 2026. These are observations from the completed checks, not guarantees about every upload, client or future provider change. Temporary raw logs, browser state, recordings and diagnostic scripts were removed after this summary was written. Repeatable checks remain in `tests/` and `scripts/`; the final edited recording remains in `demo/`.

## Provider/settings refactor verification

The service now has one YAML settings format, shared code in `app/`, and site-only files in `providers/`. The registry and framework are outside the site folder. Reusable network URLs live in each provider's `TEST_URLS`; the renamed `test_source_contract.py` tests shared behavior offline and obtains real-site URL examples from those declarations.

- **80 offline tests passed**, including discovery of a dropped-in provider file, invalid definitions, YAML inheritance/comments/validation, live-target expansion, fixed-season feed placement, private worker isolation, HTTP checks, early-EOF detection, report replacement and actual RAM-fixture relay decoding without Rex.
- The automatic provider suite attempted **16 cases** including live/experimental fixtures: **7 passed, 7 partial, 1 failed, 1 unavailable**. **14 decoded video successfully.** Bilibili failed anonymous extraction and the Twitch live fixture was offline. Missing descriptions/artwork produced partial metadata results; PeerTube and Vimeo seek samples failed independently of their successful initial decoding.
- The configured-source run sampled **20 enabled URLs: 9 passed and 11 partial**. **18 decoded successfully.** One YouTube extraction failed and one Dailymotion relay check failed. The remaining partial results describe seeking or missing source metadata. A configured collection is sampled, not exhaustively scanned by this checker.
- A real HTTP `POST /api/providers/check` returned a successful extraction/metadata/relay/decode/seek report. It used the isolated checker, without publishing to Rex.
- `--all` and `--configured` automatically maintain separate Markdown/JSON run reports. `PROVIDERS.md` is generated from actual site declarations, docstrings and handlers. Run reports replace their previous canonical files and are ignored by Git.
- Checking exposed a generic-DASH routing mistake: a Streamlink plugin claimed a finite MPD without usable stream URLs. Generic manifests now try yt-dlp first; Streamlink is a fallback for unsupported web URLs. A subsequent MPD check decoded video/audio through the relay and reported missing descriptive metadata separately.
- Extractor codec spelling `h265` is normalized to ffprobe/Rex's `hevc` name. This corrects declared metadata; it does not establish every client's HEVC playback support.
- Normal reads preserve YAML comments, duplicate/misspelled keys are rejected, and the active configuration's redundant `type: auto` entries were removed without changing its effective settings. The application contains no JSON/URL-list compatibility paths or metadata migration code.

These are bounded sample checks, not an assertion that every upload, full-length video or player works. The generated local `reports/suite.md` and `reports/configured.md` contain the individual stage results; rerun the commands in the checking guide to regenerate them.

## Addon guide and collection update — 23 October 2026

Reviewed against [Building rex addons](https://notes.debridmediamanager.com/rex/addons/) and PlexControl's static collection implementation. Source titles/artwork now use feed `display`; unchanged known source-created scalar locks can be released without overwriting different user edits. Uploaded posters are retained. Existing episode placements stay fixed, metadata refresh is bounded, selected formats are pinned, and changed delivery/probe layouts get distinct feed versions. Progressive payloads include an exact byte size when known.

- **95 offline tests passed**, including display payloads, source-lock migration, stable backfills, pinned/versioned formats, optional full/no probing, YAML compatibility, snapshots/session recovery, failed-source safety, shared ownership, capped discovery and collection GUID protection. Formatting and lint checks passed.
- A disposable instance of installed Rex **`9671faa2dda20abff12a37885ab2dc0b4bcffccb`** accepted native titles, two channel shows in one static collection, a movie in a separate collection, and a complete snapshot. Changing an indexed channel from show presentation to collection/movie presentation also produced a movie successfully.
- One real YouTube HLS item used the full ffprobe mode against its actual relay, then decoded video/audio for three seconds through that Rex executable. No MIME patch was applied.
- The existing private YAML and public commented template both validate with the additive settings. The active catalog/settings are backed up before deployment; no library reset is required.

This was focused validation, not another six-site client walkthrough or long-running live test. `probe: false` intentionally trusts extractor metadata and can miss incorrect track layouts. Manual provider checks still probe/decode to measure those limits. Source removals withdraw only orphaned catalog entries; a capped window does not remove older cached uploads. Collections remain per-library/type and are never mixed across libraries.

Daily per-site polling and unlimited discovery (`max_items: 0`) are covered by settings tests. Listings are re-read rather than resumed with an upstream cursor; cached metadata avoids unnecessary extraction. Plex Web live playback remains unresolved; native playback success does not establish browser support.

## Tested revisions

- Browser walkthrough: Rex `3bdf52bcb2e13ad414b06133768b15d6a753b1fc`, with the former manifest MIME patch.
- Isolated Rust playback and full native-transcode baseline: Rex `a9d5eedc7a7fe39e8b0b1693547d3d9b1300b0c4`, with the same patch. These results were not rerun in full against the later browser revision.
- Latest fast compatibility checks: Rex `306f4aa992700f6a3df5eabd5eca731d24dbedf5`, built by the local updater without the manifest MIME patch. The deployed service passed its health check; probes used the same built executable with disposable catalogs.
- YouTube HLS and Dailymotion HLS each passed 3-second audio/video decoding through Rex DASH, 2-second direct Part decoding and a seek request at 3 seconds. Direct Part reads redirected to Rex's HLS conversion.
- A separate-track DASH sample passed the same conversion/direct Part/seek checks. Its direct response still advertised `video/mp4`; FFmpeg decoded it successfully. This does not establish every native client's handling of that MIME label.
- TikTok progressive H.264/AAC passed 3-second audio/video decoding through Rex DASH.
- Upstream `test_feed_rejected.py` passed 8/8 and `test_feed_display.py` passed 17/17 against that executable. The service's 80 offline tests were also rerun.
- The MIME patch was removed after these checks. The full upstream native-transcode baseline, client UI walkthrough and long-running live checks were not repeated.

## Verified results

| Check | Result |
| --- | --- |
| Offline Python tests before the provider/settings refactor | 45 passed |
| Black and Ruff | Passed |
| Manifest MIME regression test | 1 passed |
| Upstream Rust universal playback tests | 20 passed; 1 ignored because it needs an explicitly configured FFmpeg executable |
| Upstream native-transcode baseline, FFmpeg 7.1.5 | 247/250 passed; failures below |
| Public-file and Git-history secret scans | No findings |
| Final demo | 80.12 seconds, 1440 × 900 H.264; full decode passed |
| Injected live outage | All 60 video frames and audio decoded across an 18-second interruption with a 15-second HTTP read timeout; no media files written |

## Playback observations

| Source | Observed behavior |
| --- | --- |
| YouTube | Computerphile played in Plex Web at 1080p and advanced beyond a seek to 60 seconds. Technology Connections was also verified in the earlier walkthrough. |
| Vimeo | Linux Foundation played continuously in Plex Web at 720p. Business of Software also passed earlier. A separate far-seek check stalled. Some listed uploads returned HTTP 401 to anonymous extraction. |
| Dailymotion | Clubic played in Plex Web at 720p. Native Rex playback passed for complete 271-second and 143-second videos, and a separate 12-second Clubic check. One TechRadar upload stalled in Plex Web. |
| PeerTube | Blender Open Movies played in Plex Web at 1080p. The PeerTube introduction collection also passed earlier. |
| TikTok uploads | NASA's microgravity dance clip played in Plex Web with decoded 576 × 1024 video. Samantha Cristoforetti also passed earlier. Several HEVC NASA uploads stalled; do not infer all TikTok formats work from the H.264 successes. |
| Twitch VOD | bashbunni played in Plex Web at 1080p and advanced beyond a seek to 90 seconds. A separate native Rex test decoded 12 minutes of a VOD, beyond its introductory footage. |
| Twitch live | A public programming broadcast decoded for 60 seconds through Rex's Part endpoint. This does not establish indefinite live playback. |
| TikTok live | Public weather broadcast decoded for 12 seconds through Rex's Part endpoint; Plex Web stalled. |
| YouTube live | NASA ISS, Monterey Bay Aquarium and Brooks Falls decoded through Rex. Twelve-second native checks passed, and three short native playback clips are included in the final demo. Plex Web live playback still stalled. |
| Bilibili | Anonymous extraction was blocked by anti-bot responses. Playback remains unverified; no authenticated workaround was added. |
| DASH sample | Separate video/audio representations relayed and decoded through Rex. |

Collections from all six accessible providers were stored as episodes under creator shows, with publication years as seasons. Single-video URLs remain movies; live providers share the Live Streams movie library. Current libraries were pinned without removing unrelated libraries. The expanded source list contains at least two creators per accessible provider; live availability can change.

## Causes found and changes made

- **Manifest MIME mismatch:** a logical `.mp4` feed label could cause Rex's Part endpoint to label an HLS body as MP4. FFmpeg then reported `moov atom not found`. The former patch preserved recognized upstream HLS/DASH MIME types. Native playback originally passed after applying it. The latest fast checks above passed without it; upstream Rex now converts direct HLS reads.
- **Muxed HLS track order:** Dailymotion and Twitch can put audio before video. Assuming the opposite produced incorrect browser stream mappings. Muxed HLS now receives a small header probe before publishing; separately described tracks use their controlled order.
- **Twitch teaser clips:** an unfiltered channel Videos page can include trailers rather than archived broadcasts. Collection URLs now request `filter=archives&sort=time`.
- **YouTube live URL handling:** `/@channel/live` was incorrectly eligible for automatic `/videos` suffixing. It is now retained as a live URL, with a regression test.
- **Separate live audio:** the live reader initially consumed only the video rendition. Separate tracks now share the configured read-ahead budget and are copy-muxed through FIFO pipes, without regular media files.
- **Packed-audio tags:** Streamlink's packed-audio parser rejected an ID3 version encountered in YouTube live audio. For explicitly separate HLS tracks, tags are preserved for FFmpeg to parse instead. The ISS audio/video check passed afterward.
- **Transient live failures:** bounded in-memory buffering, segment retries and signed-URL refresh keep the muxer's input open during recoverable interruptions. Legal MPEG-TS null packets keep an idle HTTP connection alive without fabricating frames or timestamps. Recovery is bounded; it is not DVR or unlimited reconnect support.
- **Metadata gaps:** provider descriptions, publication dates, thumbnails and available public channel bios/avatars are imported. Vimeo oEmbed supplies missing upload dates. Empty updates no longer erase existing summaries. Missing provider metadata is not invented.
- **Vimeo “advert”:** the original short Raspberry Pi example was itself a promotional clip, rather than evidence of inserted advertising. The final demo uses a different collection.
- **Logging noise:** the private runtime configuration had been left at DEBUG. About 94% of the roughly 10,224 service-log lines inspected were extractor debug messages. INFO has been restored. Most other log files came from manual probes, builds and demo recording, rather than normal service operation.

## Outstanding limitations

- Plex Web live playback remains unresolved despite successful native Rex decoding. The demo labels native live footage explicitly.
- Vimeo far seeking, some Dailymotion uploads and some TikTok HEVC uploads need further investigation. The failed recordings are not presented as successes.
- The native-transcode baseline had three failures: a far seek landed at 68.023 seconds instead of 70; the Vorbis 440 Hz tone assertion failed; and an HDR conversion color-tag assertion failed (`bt709,bt709`). These remain baseline observations, not fixes made by this adapter.
- Bilibili remains blocked/unverified. Private, subscriber-only, authenticated and DRM content are unsupported.
- Codec/client combinations outside the recorded checks, indefinite live playback, subtitles and HDR behavior are not established by these results.

## Workspace retained after cleanup

- Source, repeatable tests, probe scripts and deployment example.
- Final demo and its explanatory README.
- Active private configuration, source lists, metadata database and small rollback backups.
- Current rotating service log. Rotation is 5,000,000 bytes per file with three backups; INFO is the configured level. Systemd also receives service output.
- Virtual environment required by the installed service.

Discarded material includes raw probe/build logs, intermediate videos/screenshots, temporary edit clips, the diagnostic Rex binary, one-off debugging scripts, saved browser authentication state, public-audit scratch copies and disposable Python/Ruff caches. No active media catalog, source configuration or upstream media was deleted.


## Upstream notes review — 24 October 2026

Read the developer's notes repository and Rex changes from the preceding day,
through notes `f38b49fe` and Rex `633d8b2`. This is source/documentation review,
not a new tested-against revision; no Rex rebuild or rex-stremio update was run.

- The notes moved under `rex/`, `zurg/` and `dmm/`. The addon guide now uses
  rex-stremio as its worked example and explains one declared directory per
  library. It still documents Plex Live TV/DVR as unsupported.
- [`8051f45`](https://github.com/debridmediamanager/rex/commit/8051f4561152695b26a93c936487263e586a5ce8)
  makes duration-less sources live: rolling HLS conversion playlists without an
  end marker, waiting for future segments, and no watch/resume state. This may
  address the historical Plex Web live failures; it has not been re-tested here.
  Our continuous MPEG-TS relay is separate from a DVR/EPG. The addon now ensures
  an extractor's fragment-window length or a probe's rolling-window duration is
  not published as the total duration of a live broadcast. Finite VOD durations
  remain intact.
- [`5cf6c86`](https://github.com/debridmediamanager/rex/commit/5cf6c8660ed0af048367cc35ef8c6b8668dea532)
  detects FFmpeg's HLS `extension_picky` support and disables the strict extension
  check when available. This is relevant to playlists with extensionless or
  mismatched segment/subtitle URLs. It is a Rex playback fix, not a requirement
  to rename our libraries. This does not change our own ffprobe invocations; an
  indexing-time extension-check failure would need equivalent capability-gated
  handling in the addon as well.
- [`633d8b2`](https://github.com/debridmediamanager/rex/commit/633d8b2ae43070ec271f2f183e9becfb24b3d2e9)
  replaces track metadata when a refreshed `.m3u8` probe has changed stream
  indexes/codecs. Our conservative versioning remains compatible; we have not
  relaxed it for playlist mirrors in this review.
- Source-declared directories remain recommended by the guide. This addon still
  creates/selects libraries explicitly and sends their section keys, which the
  feed API supports. Its optional probing modes remain a documented tradeoff
  against the guide's full-probe recommendation.

The Live TV naming experiment was removed. The existing Live Streams section,
channel keys, pins and country collections were restored in place. Main retains
an independent background publisher so long backfills do not hold ready items
until the entire scan finishes. Failed pushes stay ready for retry, and delivery
is serialized to prevent scanner/publisher races.

## Batched feed publishing — 25 October 2026

Rex currently lists all feed receipts without pagination or an item-id filter.
Pending deliveries now use batches of up to 100 rows: one upsert followed by one
receipt listing per batch, rather than a full listing after every video. Missing
or deleted receipts leave rows queued; retries use the same stable feed IDs.
This reduces publication overhead without changing channel discovery or polling.

## Catalog query cost — 25 October 2026

Episode numbering previously loaded and decoded every catalog row after each saved
video. It now reads only unnumbered episodes for the affected creator and obtains
the previous maximum through an expression index. Status and offline-source
queries are indexed too. A metadata-only database-copy benchmark with 14,214 rows
measured 327.92 ms CPU for the old full decode versus 0.164 ms per unchanged
numbering call (100 calls) for the indexed implementation. This isolates numbering
work; it is not a promise about total extraction or playback CPU.
The offline suite passes 100 tests, including no JSON decoding for an already
numbered channel and confirmation that the queue query uses its index.

A subsequent live CPU profile identified repeated full yt-dlp channel enumeration
as the remaining CPU consumer during partial backfills. Successful YouTube
channel inventories are now persisted in SQLite and reused for the configured
poll interval (minimum one hour), including across restarts. Changing max_items
selects a different cached inventory; live targets bypass it. Initial discovery
and scheduled full refreshes still perform yt-dlp extraction. All 101 offline
tests pass, including cache reuse, changed limits and live bypass.

Large backfills can also involve legitimate yt-dlp extraction for thousands of
new or refresh-due videos. A configurable scanner-only average CPU budget now
yields after extraction using measured thread CPU time; playback/publishing
threads are unaffected. Calls can burst before the compensating pause. The
suite now passes 102 tests, including deterministic CPU-budget accounting.
Empty retired-version queues now return without building a catalog-wide
subquery; active retirement checks use the feed-ID index.

## Idle verification — 25 October 2026

The publisher now waits on a work event instead of polling every 15 seconds;
only failures use a five-minute retry timer. Scan deadlines persist across
restarts, and scans with no due source, delivery or snapshot return without
Rex HTTP calls or catalog/collection processing. Offline coverage verifies
these idle paths and saved deadlines; all 105 tests pass.

After pending delivery drained and the next scan was deliberately scheduled
later, a 40-second live-process sample recorded 0.0% CPU, zero disk read/write
bytes and zero read/write syscalls. Stack inspection showed both background
threads waiting and the HTTP server in select. This verifies idle behavior;
active scans and playback still consume resources.

## Sustained scan correction — 25 October 2026

The earlier idle sample measured a deliberately deferred scan, so it did not
prove normal scans finished promptly. A running scan was found to spend hours
refreshing existing uploads with per-source batches of 1,000. Upload metadata
can now opt out of age-only extraction with refresh_hours: 0, while changed
listing titles and delivery settings still request extraction. Whitespace-only
title differences are ignored. A total scan.max_extracts quota bounds upload
work across all channels without pruning incomplete inventories.

Accepted upserts with a known stable feed identity reuse their stored receipt;
changed feed versions clear it. SQLite connections are reused per thread,
avoiding per-row last-connection WAL checkpoints. Schedule-only policy changes
neither write every metadata row nor trigger publication. Explicit scan start
and finish messages distinguish ongoing work from the idle interval.

Verification after these corrections: a normal scheduled scan completed in
45.0 seconds and scheduled its next pass for one hour later. No artificial
scan deferral was used for that pass. In the following 40-second idle sample,
the process used 0.025% of one core, zero disk read/write bytes and zero
read/write syscalls, with no queued publications. All 109 offline tests pass,
including thread-local connection reuse/rollback, no receipt listing for known
identities, disabled age refresh, normalized title changes and the global quota.

## Incremental discovery and year window — 25 October 2026

YouTube channel discovery now consumes raw lazy yt-dlp entries newest-first,
stopping at a three-ID overlap. The historical metadata queue is durable and
separate from discovery progress; new discoveries are not subject to legacy
extraction caps. A channel starts at January 1 of the year it is added. Exact
video dates are checked before publication, while approximate listing dates use
a two-month overlap before the year boundary. Weekly year reconciliation does
not delete previously published media. Members-only/private listing entries are
held as restricted without repeated extraction. Explicit playback layout changes
queue a refresh rather than depending on daily archive-wide re-extraction.

The offline suite passes 120 tests. New cases cover lazy early termination,
failed discovery retaining queued work, year-boundary exclusion without pruning,
uncapped new jobs, restricted jobs not consuming backfill quota, empty listings,
and exact-date rejection before probing/publication. A real channel check read
three flat entries in 0.4 seconds without materializing the channel archive.
The initial year-window reconciliation inspected between 12 and 368 entries per
configured channel rather than every historical upload. Queue reporting is
available through `/api/discovery`.

A real repeat incremental channel check completed in 0.385 seconds. The
year-window scan and queued metadata pass then finished normally; the large
pre-existing archive was not re-extracted. Date-limited cleanup was backed up
and all explicitly removed feed IDs were verified inactive in Rex. Restricted
items are reported separately from retryable metadata failures.

Final live verification: the remaining public metadata work was drained; the
queue had no pending jobs. Members-only/private/age-gated entries were held as
restricted instead of retried as ordinary backfill. A subsequent 25-second idle
sample recorded 0.0% CPU and zero disk read/write bytes; health returned HTTP
200. Exact-date checks continued to prevent pre-boundary publication.
