# Provider definition and output contract

A provider file describes **test cases plus optional exceptions**. It is not required to implement its own downloader, web server or decoder. The [generated inventory](../reports/PROVIDERS.md) distinguishes standard sites from customized ones.

## File declarations

| Declaration | Required? | Meaning |
| --- | --- | --- |
| Module docstring | Yes, checked by the offline suite | State whether standard yt-dlp output works and explain any custom behavior. |
| `NAME` | Yes | Readable site/library label. |
| `TEST_URLS` | Yes | Nonempty list of `{ "kind": "video" / "collection" / "live", "url": "https://…" }` cases. |
| `KEY` | No | Stable provider key; defaults to the filename stem. |
| `PREFIXES` | No | Tuple/list of lowercase yt-dlp extractor-name prefixes; defaults to `(KEY,)`. Longest matching prefix wins. |
| `experimental` in a test case | No | `True` excludes that case from default network checks but lists it as skipped. |

Every non-`__` Python file in `providers/` is a provider definition. The shared registry lives in `app/provider_registry.py`. Unknown or malformed definitions are surfaced in reports. New files and their test URLs are discovered without editing a central list. Restart a running service to load provider changes.

## Minimal usable extraction output

Standard providers receive this shape from yt-dlp automatically. A custom `resolve` hook must return the same kind of dictionary. This illustrative public-video example includes the fields needed for good metadata as well as a playable format:

```python
{
    "extractor_key": "ExampleSite",
    "id": "lesson-123",
    "webpage_url": "https://videos.example/watch/lesson-123",
    "title": "Building a small weather station",
    "description": "A workshop explaining the sensors and software.",
    "channel_id": "maker-lab",
    "channel": "Maker Lab",
    "upload_date": "20261023",           # YYYYMMDD
    "duration": 120,                     # Seconds; omit for live media
    "thumbnail": "https://images.example/lesson-123.jpg",
    "is_live": False,
    "formats": [
        {
            "format_id": "1080p",
            "url": "https://cdn.example/lesson-123.mp4",
            "protocol": "https",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "mp4a.40.2",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "audio_channels": 2,
            "asr": 48000,
        }
    ],
}
```

All domains above are placeholders, not test fixtures. `id`, `title`, a canonical page URL and at least one selectable non-DRM video format are the essential contract. Creator information, date, finite duration, description and artwork determine metadata completeness. Missing descriptive fields produce a partial report; they are not invented.

Separate video/audio formats can use `vcodec: "none"` or `acodec: "none"` on their respective tracks. HLS formats use the extractor's `m3u8` protocol information; DASH retains its fragment/manifest fields. Preserve yt-dlp's codec/container metadata and transient `http_headers` when the provider needs them. The shared playback code selects compatible formats and rewrites manifests. A format cannot be declared working just because its URL exists: the relay/decoder checks must pass.

For collections, yt-dlp's flat result should contain `entries` with public webpage references (or provider-specific IDs handled by `entry_url`) and creator identity. The shared enumerator then resolves each referenced video's full metadata. An extractor returning a single item does not become a collection merely because its URL resembles a channel.

## Optional hooks

Each hook must have a docstring explaining its purpose. No hooks are needed for a provider that already supplies suitable output.

| Hook | Return / behavior | Stage |
| --- | --- | --- |
| `prepare(url)` | `(url, options_dict)`; change a public embed URL or yt-dlp request options. | Before yt-dlp extraction |
| `resolve(url, *, flat=False)` | Full info dictionary to override extraction; `None` to use standard yt-dlp. | Extraction |
| `enrich(info, *, flat=False)` | Mutate `info` to fill fields from a public metadata endpoint. | After extraction |
| `creator(ident)` | Optional `{ "description": …, "art": …, "year": … }`. | Collection metadata |
| `collection_url(url)` | Select the provider's collection/archive URL. | Enumeration |
| `entry_url(entry)` | Convert an ID-only flat entry to a canonical webpage URL. | Enumeration |
| `targets(url)` | Initial URL list for a configured source, e.g. Twitch archives plus channel root. | Polling |
| `live_url(url)` | Channel's live URL or `None`; used by the YAML `check_live` setting. | Live discovery |
| `live_status(info)` | Whether this info is truly a live broadcast, rather than an ongoing saved VOD. | Classification |

A `resolve` hook is the place for a genuinely different public extractor. A `prepare` hook is enough when only a URL or request option differs. Shared HTTP serving, Rex publication and buffering stay in `app/` rather than being copied into every site file.

## Normalized output and verification

`app/source.py` converts source output into a stable item containing provider/source IDs, canonical page URL, title, creator, summary, publication date, duration, thumbnail, kind and live status. Applying YAML adds the selected season, library and display overrides. IDs remain based on source identity, not the overridden title. Format URLs, request headers and temporary cookies remain in memory rather than the stored item.

`app/playback.py` produces the selected delivery plan and declared media information. The checker compares those declared track indices/codecs with ffprobe's observed relay tracks, then decodes actual frames. Reports intentionally expose safe selected metadata and capability results rather than the full transient plan.

See [CHECKING_PROVIDERS.md](CHECKING_PROVIDERS.md) for a sample partial-result report. Normalized metadata, relay readability, decoded playback, thumbnail access and seeking are separate outcomes. A custom handler returning the dictionary above is only a starting point; a successful network check demonstrates which parts actually work.
