# Adding a yt-dlp provider

This guide is for **rex-youtube**. Most sites supported by yt-dlp can use the existing extraction, metadata and relay code. Start with a public video URL; add special handling only if testing reveals something the shared code cannot handle.

Run the commands below from your rex-youtube installation directory. ExampleSite names and `videos.example` URLs are placeholders—replace them with the actual provider and public URLs.

## 1. Try adding the URL

Add a public channel, playlist or video to `config/sources.txt`:

```text
https://videos.example/channels/maker | Maker Lab
```

The optional text after `|` overrides the creator/show name. Then restart:

```sh
systemctl restart rex-youtube
```

If you run the Python process directly, stop and start it instead. The next scan extracts the URL and creates the appropriate Rex library automatically:

| URL extracted as | Result in Rex |
| --- | --- |
| Channel or playlist | Creator show in the provider library; years become seasons and uploads become episodes |
| Single video | Movie in the provider’s Videos library |
| Live broadcast | Temporary movie in the shared Live Streams library |

The extractor must expose a channel/playlist as a collection for it to become a show. A display-name override alone does not turn a single video into a collection. Missing publication dates use season 0.

Existing defaults backfill five entries per poll, every 15 minutes. A partially populated channel may simply be waiting for another batch.

## 2. Check the extractor identity

If the site needs a clearer library name or channel/video extractors are ending up under different providers, inspect a representative video:

```sh
venv/bin/python - 'https://videos.example/watch/123' <<'PY'
import sys
import providers

info = providers.extract(sys.argv[1])
print("Extractor:", info.get("extractor_key") or info.get("extractor"))
print("Provider:", providers.provider(info.get("extractor_key") or info.get("extractor")))
print("Title:", info.get("title"))
print("Creator:", info.get("channel") or info.get("uploader"))
PY
```

This extracts metadata without downloading a media file. It prints selected fields, rather than dumping signed playback URLs or temporary cookies.

In `providers.py`, add an entry to `SOURCES` if needed:

```python
SOURCES = {
    # Existing entries stay here.
    "examplesite": "Example Site",
}
```

The key is a lowercase **extractor-name prefix**, not a hostname. An `examplesite` entry groups names such as `ExampleSite` and `ExampleSiteChannel` under one provider. The value supplies the readable library name. Unknown extractor identities already have a fallback, so this entry is unnecessary when the existing name and grouping are suitable.

Choose the key before populating a production library: it becomes part of persistent feed IDs. Renaming it later can create duplicate identities. If the extractor names do not share a suitable prefix, add a narrow explicit mapping in `provider()` rather than a hostname-based routing system.

## 3. Prove playback

A successful title lookup does not prove that Rex or a player can read the media.

In `scripts/probe_sites.py`, add a stable, public video to `EXAMPLES`:

```python
EXAMPLES = {
    # Existing entries stay here.
    "examplesite": "https://videos.example/watch/123",
}
```

For channel support, also add its collection URL to `COLLECTIONS` using the same key. Then run:

```sh
# Resolve and decode the relay; compare its tracks with the feed metadata.
venv/bin/python scripts/probe_sites.py --site examplesite

# Also publish into a disposable Rex instance and decode through Rex.
venv/bin/python scripts/probe_sites.py --site examplesite --rex-bin /path/to/rex-server

# Check creator/show, year/season and episode placement too.
venv/bin/python scripts/probe_sites.py --site examplesite --collections --rex-bin /path/to/rex-server
```

Add the dictionary entries **before** running these commands: an unknown `--site` name selects no examples. A real run should print a JSON result for the provider with `"result": "passed"`. The current probe requires both video and audio; a silent camera needs a deliberately adapted check.

The Rex checks use a temporary catalog, not your running libraries. Use a Rex executable with the playback capabilities/patch described in the main README. Finally, try the installed service in your actual client: check playback beyond the intro, duration, seeking, episode ordering and thumbnails. Native-client and Plex Web results can differ.

## 4. Add a small special case only when necessary

The shared code already handles formats, headers, metadata, HLS/DASH relaying and library creation. Inspect these files when a check fails:

| File | What belongs there |
| --- | --- |
| `providers.py` | Extractor identity, collection URLs, missing provider metadata or public embed handling |
| `playback.py` | Format selection, codec/track metadata and manifest generation |
| `live.py` | Bounded live buffering and reconnection |
| `service.py` | Shared persistence, Rex publication and HTTP serving |

For example, Vimeo uses a public embed endpoint and oEmbed dates, while Twitch collections explicitly select archived VODs. Keep comparable exceptions close to extraction and leave the shared publisher unchanged where possible. Use metadata the source actually supplies; do not guess an ID, episode number or codec to make a failed test pass.

Add an offline regression test in `tests/test_sources.py` or the relevant existing test file for any new behavior. Mock provider requests; keep live websites in the opt-in network probes. Run:

```sh
venv/bin/black --check service.py providers.py playback.py live.py scripts tests
venv/bin/ruff check service.py providers.py playback.py live.py scripts tests
venv/bin/python -m unittest discover -s tests
```

## 5. Document and share the example

Add a verified public URL to `config/sources.example.txt` and record the provider’s tested behavior and limitations in the README. Update `docs/TEST_FINDINGS.md` when the results change. Keep active config, tokens, browser state, logs and diagnostic recordings out of Git.

This example supports anonymous public media. A yt-dlp extractor existing for a site does not guarantee anonymous access, a compatible codec, seekable playback or working live streams. Login-only and DRM content remain outside this adapter’s scope. See [the current findings](TEST_FINDINGS.md) before treating a limitation as a new-provider bug.
