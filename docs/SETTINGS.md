# YAML settings

Edit `config/config.yml`, then restart `rex-youtube`. Comments are supported. The service reads this file without rewriting it, so comments and formatting remain intact. `config/config.yaml` is also accepted, but keep only one of those files.

Start from the [commented example](../config/config.example.yml). YAML is the only supported settings format. This is a small example to copy and modify.

## Shape and inheritance

```yaml
server:
  port: 8790
  log_level: INFO
rex:
  url: "http://REX_HOST:REX_PORT"
  source_id: youtube
scan:
  snapshot_hours: 6
  interval_seconds: 900
  batch_size: 5
  max_items: 1000
playback:
  max_height: 1080
  live_buffer_mb: 16

defaults:
  year_seasons: true
  check_live: false

sites:
  youtube:
    library: Learning
    movie_library: Learning Films
    live_library: Live Streams
    max_items: 50
    sources:
      - url: https://videos.example/channel
        type: show
        name: My Channel
        year_seasons: false
        season: 1
        max_items: 20
        check_live: true
      - url: https://videos.example/watch/123
        type: movie
        name: My Film
```

The example URLs above are placeholders. Use real public URLs for the site. Inheritance is **built-in defaults → global settings/defaults → site → source**. The source's `max_items: 20` therefore overrides the site's 50 and the global 1000.

Site keys normally match provider filenames, such as `youtube`, `vimeo` or `twitch`. An otherwise supported yt-dlp site can also be configured before a custom provider file exists. Site files supply reusable tests and optional handlers; the YAML source list is your actual selection of channels/items, not another test fixture list.

## Global settings

| Section / setting | Purpose |
| --- | --- |
| `server.host`, `server.port` | Relay listener; defaults to `0.0.0.0:8790`. |
| `server.public_url` | Optional address Rex should use to reach this relay; empty derives the local interface address. |
| `server.log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`; defaults to INFO. |
| `rex.url` | Your Rex HTTP address, including its configured port when needed. |
| `rex.token` | Optional Rex credential. `REX_TOKEN` in the environment takes precedence. Keep credentials out of Git. |
| `rex.source_id` | Stable feed identity. Retain the existing value when upgrading. |
| `scan.interval_seconds` | Poll interval, default 900 seconds. |
| `scan.batch_size` | New/refreshed items processed per source per poll, default 5. |
| `scan.max_items` | Most recent collection entries considered per poll, default 1000. |
| `playback.max_height` | Preferred maximum video height, default 1080. |
| `playback.live_buffer_mb` | Read-ahead budget per live playback, default 16 MiB; range 1–64. |

The service keeps at most four live playback relays active. The buffer is not a DVR or a disk cache.

## Site and source settings

| Setting | Where | Meaning |
| --- | --- | --- |
| `enabled` | Site or source | `false` stops scanning that entry without deleting its settings. Items owned only by that source are withdrawn after a successful reconciliation. |
| `library` | Site | Show-library name; defaults to the provider's readable name. |
| `movie_library` | Site | Standalone/movie-collection library; defaults to `<Provider> Videos`. |
| `live_library` | Site | Live-library name; defaults to the shared `Live Streams`. |
| `sources` | Site | List of channel, playlist, video or live URL entries. |
| `url` | Source | Public URL to extract. |
| `type` | Defaults, site or source | `auto` follows the extractor; `show` groups items as episodes; `movie` treats uploads as movies; `live` expects a live broadcast; `collection` groups uploads as movies by default. |
| `name` | Source | Override a show's name or a standalone/live item's title. It does not rename every movie in a collection to the same title. |
| `year_seasons` | Defaults, site or source | `true`: publication years are seasons. `false`: episodes share the configured season and receive stable episode numbers. New backfilled uploads append instead of renumbering existing ones. |
| `season` | Defaults, site or source | Season used when `year_seasons: false`; default 1. |
| `max_items` | Defaults, site or source | Collection discovery limit, overriding `scan.max_items`. |
| `batch_size` | Defaults, site or source | Per-source work limit, overriding `scan.batch_size`. |
| `max_height` | Defaults, site or source | Per-source quality preference, overriding `playback.max_height`. |
| `check_live` | Defaults, site or source | Also check a channel's live endpoint when the provider supplies `live_url`. YouTube, Twitch and TikTok have helpers. |

`interval_seconds` on a site or source sets its successful-poll interval (seconds); zero uses the worker interval. Partial or failed scans retry on the next worker cycle. Scheduling survives restarts. Live sources can override a daily site interval with 900 seconds.

Discovery re-reads the listing; it does not use an upstream since-last-poll cursor. Cached items skip extraction until refresh is due. `batch_size` bounds new/refresh work per round.

`max_items: 0` discovers all available entries. `max_items` limits discovery, not retention: it does not remove older items already in Rex. Decreasing it does not erase older cached uploads. Disabling/removing a source withdraws rows it alone owns after a successful polling round. Undated year-based episodes use season 0. Show, movie and live libraries are separate options because Rex library types cannot be mixed arbitrarily.

For a known live URL, set `type: live`; this enables checking that entry even when the site's `check_live` default is false. The live-check toggle does not invent an endpoint for a site that lacks a discovery helper. Add an explicit live URL for such a provider. Only a confirmed offline result removes a transient live item; temporary extraction/network failures preserve it.

Name, season and library changes are applied to existing identities included in subsequent scans. Changes that require new extraction are processed within the configured batch limit. Keep provider keys and `rex.source_id` stable to preserve IDs and watch state. Do not configure the same item repeatedly with conflicting overrides.

## Validate the actual selection

```sh
venv/bin/python scripts/check_providers.py --configured
```

This reads enabled YAML sources and inherited options, including any enabled channel live checks. It uses the real relay and FFmpeg without publishing to Rex. Results automatically replace `reports/configured.md` and `reports/configured.json`, as well as the latest-run report. Use `--config-file /path/to/settings.yml` to test a draft configuration without installing it. A configured collection check samples one video; it does not validate every episode in that channel.

Unknown keys, duplicate YAML keys, invalid types and out-of-range values are rejected before scanning. Quoted `"false"` is a string, not a boolean: use `false` without quotes for toggles.

## Probing and refresh

Existing YAML files remain valid; all new keys are optional and inherit through defaults → site → source. `probe` accepts:

| Value | Indexing behavior |
| --- | --- |
| `headers` (default) | Preserve the previous behavior: a small muxed-HLS track-layout check; separate tracks use their controlled order. |
| `true` | Run bounded ffprobe against the actual HTTP relay and send the whole format/streams/chapters response to Rex. No media is cached to disk. |
| `false` | Do not invoke ffprobe during indexing. Build the feed metadata from the selected extractor formats. |

Set `sites.youtube.probe: false`, for example, to avoid probing that site's indexed uploads. The checker still probes/decodes when explicitly asked to test playback. With probing disabled, extractor metadata may be incomplete or track-order assumptions wrong; supplying arbitrary codec/duration information is not safe. Rex uses it for playback decisions. Compatible selected formats are pinned on URL refresh; a changed representation gets a new feed version and file label. A playback request refuses mismatched formats until a poll publishes the new version.

`refresh_hours` defaults to 24 and controls revisiting existing uploads for changed titles, descriptions and artwork. Work shares `batch_size` with indexing. Source fields are sent using Rex feed `display`; new publications do not create user-edit locks. Existing user edits and uploaded posters are retained. Known unchanged scalar locks from the older source-edit implementation are released when moving to feed display.

`scan.snapshot_hours` defaults to 6. Snapshots include the full cached inventory, not just the latest discovery window, and are skipped after incomplete scans or delivery failures. An uncapped, successful listing can withdraw disappeared uploads; failed or empty listings cannot erase a channel. Removing a configured source withdraws only items not also owned by another configured source. No upstream video is deleted. Lost snapshot sessions restart at part zero; shrink protection is never forced off.

## Rex collections

Collections group existing shows or movies without changing what they are. Like PlexControl, this addon creates a separate collection per library and member type; a movie and a TV library cannot share one cross-library collection.

```yaml
sites:
  youtube:
    library: YouTube
    movie_library: YouTube Videos
    live_collection: YouTube Live # true (default) uses the site name; false disables it.
    probe: false                 # Optional per-site indexing choice.
    sources:
      - url: https://www.youtube.com/@level1techs/videos
        type: show
        collection: Level1Techs
      - url: https://www.youtube.com/@Level1Linux/videos
        type: show
        collection: Level1Techs
      - url: https://www.youtube.com/@level1news/videos
        type: collection         # Individual movies grouped into a collection.
        collection: Level1Techs
        collection_type: movie   # Default for type: collection; show also works.
```

Merge this example into the existing `sites` mapping; do not configure one URL twice with conflicting types. The first two channels stay distinct shows in a Level1Techs TV collection. The third contributes films to a separate Level1Techs movie collection.

`collection` accepts a name, `true` (use the channel name) or `false` (no collection). `type: collection` implies a channel-named collection when no name is given; `collection_type` chooses `movie` (default) or `show`. A plain `type: show` or `type: movie` can independently set `collection` too. `live_collection` has the same name/boolean options, defaults to true, and groups live items by site in their live library. All these options can be set in defaults, at site level or per source.

After a partial scan, reconciliation can add known collection members but does not remove memberships or collections. Only collections created and recorded by this addon are reconciled. A stored GUID must match before editing or deletion. Disabling a group removes its managed collection, never its media or somebody else's collection.
