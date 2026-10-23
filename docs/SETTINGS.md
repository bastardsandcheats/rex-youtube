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
| `enabled` | Site or source | `false` stops scanning that entry without deleting its settings. Existing persistent catalog items are retained. |
| `library` | Site | Show-library name; defaults to the provider's readable name. |
| `movie_library` | Site | Standalone/movie-collection library; defaults to `<Provider> Videos`. |
| `live_library` | Site | Live-library name; defaults to the shared `Live Streams`. |
| `sources` | Site | List of channel, playlist, video or live URL entries. |
| `url` | Source | Public URL to extract. |
| `type` | Defaults, site or source | `auto` follows the extractor; `show` groups items as episodes; `movie` treats uploads as movies; `live` expects a live broadcast. |
| `name` | Source | Override a show's name or a standalone/live item's title. It does not rename every movie in a collection to the same title. |
| `year_seasons` | Defaults, site or source | `true`: publication years are seasons. `false`: episodes share the configured season and remain oldest-to-newest. |
| `season` | Defaults, site or source | Season used when `year_seasons: false`; default 1. |
| `max_items` | Defaults, site or source | Collection discovery limit, overriding `scan.max_items`. |
| `batch_size` | Defaults, site or source | Per-source work limit, overriding `scan.batch_size`. |
| `max_height` | Defaults, site or source | Per-source quality preference, overriding `playback.max_height`. |
| `check_live` | Defaults, site or source | Also check a channel's live endpoint when the provider supplies `live_url`. YouTube, Twitch and TikTok have helpers. |

`max_items` limits discovery, not retention: it does not remove older items already in Rex. Decreasing it or disabling a source does not erase the persistent catalog. Undated year-based episodes use season 0. Show, movie and live libraries are separate options because Rex library types cannot be mixed arbitrarily.

For a known live URL, set `type: live`; this enables checking that entry even when the site's `check_live` default is false. The live-check toggle does not invent an endpoint for a site that lacks a discovery helper. Add an explicit live URL for such a provider. Only a confirmed offline result removes a transient live item; temporary extraction/network failures preserve it.

Name, season and library changes are applied to existing identities included in subsequent scans. Changes that require new extraction are processed within the configured batch limit. Keep provider keys and `rex.source_id` stable to preserve IDs and watch state. Do not configure the same item repeatedly with conflicting overrides.

## Validate the actual selection

```sh
venv/bin/python scripts/check_providers.py --configured
```

This reads enabled YAML sources and inherited options, including any enabled channel live checks. It uses the real relay and FFmpeg without publishing to Rex. Results automatically replace `reports/configured.md` and `reports/configured.json`, as well as the latest-run report. Use `--config-file /path/to/settings.yml` to test a draft configuration without installing it. A configured collection check samples one video; it does not validate every episode in that channel.

Unknown keys, duplicate YAML keys, invalid types and out-of-range values are rejected before scanning. Quoted `"false"` is a string, not a boolean: use `false` without quotes for toggles.
