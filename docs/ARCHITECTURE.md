# Architecture

The site folder contains only sites. The shared application does extraction orchestration, metadata placement, publication and relaying. There is no separate playback engine for every provider.

```text
service.py                 Small service launcher
app/
  config.py                YAML validation and inheritance
  provider_registry.py     Automatic discovery of provider files
  source.py                Shared yt-dlp extraction and metadata normalization
  playback.py              Format selection, media metadata and manifest rewriting
  live.py                  Bounded RAM buffering and live copy-muxing
  collections.py           Owned static Rex collections, scoped per library/type
  service.py               SQLite catalog, Rex feed client, HTTP server and poller
  checks.py                Isolated, read-only provider checks
  reporting.py             Generated inventories and Markdown/JSON reports
providers/
  youtube.py               Test URLs and YouTube URL helpers
  vimeo.py                 Test URLs and public embed/metadata helpers
  ...                      Every non-__ Python file is a provider definition
scripts/
  check_providers.py       Check a URL, all discovered sites or configured sources
  probe_sites.py           Optional integration checks using a disposable Rex
  probe_live_buffer.py     Repeatable synthetic interruption test
config/config.example.yml  Commented settings template
reports/PROVIDERS.md        Generated site/handler inventory and diagram
reports/latest.md,json     Latest check; automatically replaced
reports/suite.md,json      Latest complete --all run; automatically replaced
reports/configured.md,json Latest --configured run; automatically replaced
```

## Indexing and metadata

```mermaid
flowchart TD
    yaml["config/config.yml<br/>Sites, sources and overrides"] --> poll["Poll enabled sources"]
    files["providers/*.py"] --> registry["Automatic registry<br/>app/provider_registry.py"]
    registry --> poll
    poll --> targets["Optional URL helpers<br/>targets / collection_url / live_url"]
    targets --> extract["Shared extraction in app/source.py"]
    extract --> prepare["Optional prepare: URL or request options"]
    prepare --> ytdlp["Standard yt-dlp extractor"]
    extract --> resolve["Optional resolve override<br/>Twitch live uses Streamlink"]
    ytdlp --> enrich["Optional enrich / creator metadata hooks"]
    resolve --> enrich
    enrich --> normalize["Normalize metadata<br/>Apply name, type and season settings"]
    normalize --> plan["Choose formats and verify muxed HLS track order"]
    plan --> sqlite["Small metadata SQLite catalog"]
    sqlite --> feed["Publish placement and display<br/>through Rex APIs"]
    feed --> rex["Rex library and clients"]
```

`resolve` is optional. With no such hook, extraction uses yt-dlp. For a generic URL yt-dlp cannot handle, a matching Streamlink live plugin is tried. Ordinary DASH manifests stay on the yt-dlp path. Provider-specific URL knowledge stays in the provider file; the generic poller does not contain a list of hostnames.

For example, **YouTube uses standard yt-dlp for individual videos**. Its URL helpers select uploads, locate an optional channel live endpoint and expand ID-only flat entries. It has no custom video decoder. Vimeo's helpers are different: an anonymous player URL, public oEmbed dates and public creator information. Twitch uses an extraction override for live broadcasts and explicitly selects archived VODs for channel collections.

The [generated provider inventory](../reports/PROVIDERS.md) lists every actual hook with its docstring and a generated handler map. This is regenerated from the code when checks run, so the diagram/table need not be manually updated when another site is added.

## Playback

```mermaid
flowchart TD
    client["Client → Rex"] --> http["app/service.py HTTP relay"]
    http --> fresh["Resolve fresh signed URLs in RAM"]
    fresh --> progressive["Progressive media<br/>Bytes and Range requests"]
    fresh --> adaptive["HLS / DASH<br/>Rewrite manifests; relay segments"]
    fresh --> live["Live: bounded Streamlink read-ahead"]
    live --> tracks["Separate tracks, when needed: FIFO copy-mux"]
    tracks --> ts["FFmpeg copy-mux to MPEG-TS; no encoding"]
    progressive --> rex["Rex's normal playback path"]
    adaptive --> rex
    ts --> rex
```

Media is not downloaded to a cache. The service persists metadata and configuration. Live read-ahead is in RAM; separate-track FIFO pipes do not store regular media files. Rex may use its own normal transcoder output. Saved demo clips are demonstration artifacts, not a service cache.

## Provider checks and generated reports

```mermaid
flowchart TD
    input["Start a check<br/>CLI URL, suite or HTTP request"] --> child["Fresh bounded worker process"]
    child --> extraction["Extract and normalize<br/>using the service settings"]
    extraction --> temp["Temporary metadata DB<br/>and loopback HTTP relay"]
    temp --> probe["ffprobe<br/>Check codecs and tracks"]
    probe --> decode["FFmpeg<br/>Decode a short sample"]
    decode --> seek["Recorded video<br/>Attempt a seek sample"]
    seek --> report["Report each stage<br/>and playback capability"]
    report --> files["Generate reports<br/>Markdown and JSON"]
```

The normal checks do not invoke Rex, publish to its feed, or alter the running service's catalog. Process isolation also prevents a check from replacing the active relay's URL caches or configuration. Each case has an overall deadline; timed-out worker process groups are terminated. A collection check inspects one representative video and states that scope explicitly.

The stages are discovery, extraction, metadata completeness, format selection, artwork access, relay headers, declared-versus-observed feed tracks, decoded playback and seeking. Missing descriptions/posters can produce a partial result while decoded playback still passes. An offline source is distinct from a source that extracted successfully but failed to decode. Skipped live/experimental fixtures are listed explicitly.

The [checking guide](CHECKING_PROVIDERS.md) explains commands, the HTTP endpoint and result levels. The [provider contract](PROVIDER_CONTRACT.md) shows the output a custom extractor must supply. Offline regression tests separately cover configuration, identity, normalization, HTTP behavior, worker isolation, report generation and provider discovery.

## Refresh and collection ownership

Source URLs own catalog rows through `item_sources`. Failed or capped listings retain cached inventory. After a successful round, unowned rows are withdrawn; complete snapshots reconcile all published cached items. `retired_versions` persists withdrawals when a delivery layout changes. Format IDs/layout are pinned while only signed URLs refresh. Episode numbers are allocated once per placement group.

`app/collections.py` follows the static collection create/member APIs used by PlexControl. It maps episode memberships to their shows, scopes groups by library/type and records each created collection's GUID before changing membership. User collections are not adopted. Probe mode and collection presentation are independent YAML options.

## Incremental YouTube channel handler

```mermaid
flowchart TD
    poll[Scheduled channel check] --> pages[Lazy newest-first yt-dlp entries]
    pages --> stop[Stop at known-ID overlap or January boundary]
    stop --> inventory[SQLite discovered IDs]
    inventory --> fresh[New uploads and explicit refresh jobs]
    inventory --> history[Separate historical metadata queue]
    fresh --> metadata[Extract metadata and verify exact date]
    history --> metadata
    metadata --> feed[Publish new or changed rows to Rex]
    restricted[Members-only or private listing] --> held[Restricted state: no repeated extraction]
    pages --> restricted
    weekly[Weekly year-window reconciliation] --> pages
```

`app/discovery.py` owns the durable discovery inventory and metadata queue.
`app/source.py:iter_youtube_uploads` consumes raw lazy extractor entries rather
than materializing a playlist. Existing providers retain their shared handler.
The January boundary is stored when a channel is added, and existing media is
never removed merely because an incremental pass does not return it. Changed
playback layouts queue an explicit refresh and wake the scanner; routine polling
does not reopen unchanged uploads.
