# Add a provider: grab, test and hack

Start with a URL. You do not need to add code or edit settings just to find out whether a site works:

```sh
venv/bin/python scripts/check_providers.py 'https://videos.example/watch/123'
```

Use the real public URL instead of the placeholder. The checker exercises extraction, metadata, the actual HTTP relay, decoded playback and seeking without Rex. Read the individual results: metadata extraction alone is not proof of playable media.

## Standard yt-dlp provider: one small file

If the site already returns usable yt-dlp output, create `providers/examplesite.py`:

```python
"""Example Site uses standard yt-dlp extraction and the shared relay.

No custom extraction, metadata or playback handlers are required.
"""

NAME = "Example Site"
TEST_URLS = [
    {"kind": "video", "url": "https://videos.example/watch/123"},
    {"kind": "collection", "url": "https://videos.example/channels/maker"},
]
```

Those URLs are placeholders too. Choose stable public test URLs containing the content you intend to support. The filename supplies the provider key and extractor-name prefix: `examplesite.py` groups extractor names beginning with `ExampleSite`. Optional `KEY` and `PREFIXES` declarations handle different extractor naming; see the [contract](PROVIDER_CONTRACT.md).

**That is the whole provider file for a standard site.** Every non-`__` Python file in `providers/` is discovered automatically, including by the test suite. The folder contains no registry or framework modules. Missing/invalid declarations become reported definition failures instead of hiding other sites.

```sh
venv/bin/python scripts/check_providers.py --provider examplesite
venv/bin/python scripts/check_providers.py --all
```

These commands get their URLs from the provider files. Do not copy another test URL list into a script or offline unit test. Add a live fixture with `kind: "live"` when appropriate; use `--include-live` to attempt volatile broadcasts. Experimental cases can declare `experimental: True` and are attempted with `--include-experimental`.

## Add the actual channel/item to YAML

The provider file holds reusable tests. Your private YAML holds the channels/items you want in Rex:

```yaml
sites:
  examplesite:
    library: Workshops
    movie_library: Workshop Films
    sources:
      - url: https://videos.example/channels/my-channel
        type: show
        name: My Workshop
        year_seasons: false
        season: 1
        max_items: 20
      - url: https://videos.example/watch/my-film
        type: movie
```

Omit `type` to use the default `auto`. Restart the service after changing YAML or adding a site file. Check your actual selection independently of the reusable fixtures:

```sh
venv/bin/python scripts/check_providers.py --configured
```

See [settings](SETTINGS.md) for inheritance, live checks, library overrides and the remaining options. You do not have to maintain a YAML test list as well: tests live once in the provider file, and `--configured` reads the actual YAML entries.

## When a custom handler is needed

Keep the exception in that site's file. The [generated inventory](../reports/PROVIDERS.md) shows exactly which existing sites use custom handlers and why. For example:

- **YouTube:** standard yt-dlp video extraction; only channel/live URL selection and ID-to-URL helpers are custom.
- **Vimeo:** public embedded-player selection, missing upload-date enrichment and optional public creator metadata.
- **Twitch:** Streamlink live extraction, archived-VOD URL selection and public creator metadata.
- **A provider without hooks:** standard yt-dlp extraction, normalization and relaying throughout.

The optional hook signatures and minimum raw output are in [PROVIDER_CONTRACT.md](PROVIDER_CONTRACT.md). Give each hook a docstring explaining the site-specific reason it exists; discovery validates that documentation. Keep format/manifest handling in `app/playback.py` and live buffering in `app/live.py` when it is shared across sites.

Do not guess a codec, ID, title, season or episode number to make a test pass. If a site omits metadata or anonymous access is blocked, the report should say so. This example uses public anonymous media; account/private/DRM workflows are outside its current implementation.

## Check and document the change

```sh
venv/bin/black --check service.py app providers scripts tests
venv/bin/ruff check service.py app providers scripts tests
venv/bin/python -m unittest discover -s tests
venv/bin/python scripts/check_providers.py --provider examplesite
```

The offline suite tests shared contracts, configuration, HTTP behavior and discovery without contacting websites. It also validates each discovered provider file. Network checks use that file's URLs and automatically generate Markdown/JSON reports. Add a focused offline regression for custom behavior; ordinary new-provider files need no central registration or duplicated fixture dictionary.

Read [the architecture diagrams](ARCHITECTURE.md), [checking/report guide](CHECKING_PROVIDERS.md) and [current findings](TEST_FINDINGS.md). A few seconds of FFmpeg decoding does not establish full-length or client compatibility: try duration, seeking and playback beyond intros in your intended client before claiming those work. Add a verified public configuration example when useful, and keep real settings, tokens and run reports out of Git.
