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

Reviewed against [Building rex addons](https://notes.debridmediamanager.com/guides/rex-addons/) and PlexControl's static collection implementation. Source titles/artwork now use feed `display`; unchanged known source-created scalar locks can be released without overwriting different user edits. Uploaded posters are retained. Existing episode placements stay fixed, metadata refresh is bounded, selected formats are pinned, and changed delivery/probe layouts get distinct feed versions. Progressive payloads include an exact byte size when known.

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
