# Checking a provider

You can check any public URL without changing the service settings or adding a provider file:

```sh
venv/bin/python scripts/check_providers.py 'https://videos.example/watch/123'
```

Replace the placeholder with the actual URL. This works with the service stopped and without a private settings file too. It extracts metadata, serves the selected media through an isolated instance of the real relay, probes its streams, decodes four seconds and attempts a finite seek. It does not start or contact Rex, modify the active catalog, save video files, or add the URL to your settings.

## Service endpoint

Ask the running service locally:

```sh
curl -sS http://127.0.0.1:8790/api/providers/check \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://videos.example/watch/123","seconds":4}'
```

Use your configured relay port. Remote requests require `X-Plex-Token` matching the service's configured Rex token; loopback requests do not. Each request is bounded, and only two HTTP checks can run concurrently. A check can take up to three minutes; it returns its partial results if it reaches the deadline. `GET /api/providers` lists discovered site definitions, their test URLs, custom handlers and definition errors.

HTTP 200 means the check completed and returned a report; inspect the report's `result` and `capabilities` to determine what worked. Invalid requests return 400, unauthorized remote checks 403, and excess concurrent work 429.

## Automatic provider suite

```sh
# Discover every provider file and its test cases.
venv/bin/python scripts/check_providers.py --all

# Include fixtures whose availability is volatile or experimental.
venv/bin/python scripts/check_providers.py --all --include-live --include-experimental

# Pick one provider, or inspect the automatically generated inventory.
venv/bin/python scripts/check_providers.py --provider youtube
venv/bin/python scripts/check_providers.py --list

# Test actual enabled YAML sources and their inherited settings.
venv/bin/python scripts/check_providers.py --configured
```

Every non-`__` Python file in `providers/` is discovered. No central URL dictionary needs editing. Invalid modules appear as definition failures without hiding healthy providers. Live and experimental fixtures skipped by the default suite are listed explicitly. A configured run tests your enabled URLs, including configured live checks, rather than the public fixture URLs.

## Result levels

| Result | Meaning |
| --- | --- |
| `passed` | The attempted checks passed; irrelevant checks may be skipped. |
| `partial` | Some stages worked, but metadata is incomplete or another stage failed. |
| `failed` | The check could not establish a successful stage, or a definition is invalid. |
| `unavailable` | The extractor positively reported unavailable/offline. This does not prove the provider is unsupported. |
| `skipped` | A fixture was deliberately not attempted. |

The separate `level` is a quick capability summary: `decoded_sample`, `decoded_with_gaps`, `relay_readable`, `metadata_only`, `unavailable` or `not_verified`. Always check the individual stage results too. For example:

```json
{
  "result": "partial",
  "level": "decoded_with_gaps",
  "capabilities": {
    "metadata_extracted": true,
    "playable": true,
    "seekable": false,
    "feed_metadata_valid": true,
    "metadata_complete": false
  },
  "checks": {
    "metadata": {
      "status": "partial",
      "summary": "Metadata has gaps.",
      "missing": ["thumbnail"]
    },
    "decode": {
      "status": "passed",
      "summary": "Decoded the requested sample.",
      "frames": 120,
      "seconds_decoded": 4.0,
      "seconds_requested": 4.0,
      "exit_code": 0
    },
    "seek": {
      "status": "failed",
      "summary": "Decoding failed or ended before the requested sample completed."
    }
  }
}
```

This is an illustrative excerpt: video playback worked, the thumbnail was missing, and seeking failed. It is not an unconditional “supported” result.

## What is checked

- Discovery and extraction, including whether a collection exposes a sample item.
- Required IDs/title/page URL and descriptive fields such as summary, date, creator, thumbnail and finite duration.
- Format selection and thumbnail readability.
- Actual video stream headers through the relay.
- Feed-declared stream indices and codecs versus observed tracks.
- Decoded frames and sample duration, catching an early EOF even when FFmpeg exits successfully.
- A short seek sample when a sufficient finite duration is known; live seeking is skipped.

A collection check samples one representative item. Short decoding and seeking samples do not guarantee full-length playback, long-term live recovery, Plex Web compatibility or every upload's codec. Missing provider metadata is reported rather than fabricated. Reports omit signed playback URLs, request cookies, credentials and raw extractor errors.

## Reports and exit codes

Reports are generated automatically:

| File | Contents |
| --- | --- |
| `reports/PROVIDERS.md` | Generated provider list, fixtures, handler descriptions and Mermaid handler map. |
| `reports/latest.md` / `.json` | Most recently completed or partially completed run. |
| `reports/suite.md` / `.json` | Most recent `--all` run. |
| `reports/configured.md` / `.json` | Most recent `--configured` run. |

Canonical report files are replaced rather than accumulating recordings/logs. Partial suite results are saved after each case. Run reports are ignored by Git; the generated static inventory is shareable. Add `--json` for JSON on stdout or `--report-dir DIR` for a different destination. Exit 0 means attempted cases passed, 1 means a failed stage/unavailable case, and 2 means partial results due only to completeness warnings. A skipped fixture alone is not a failure.

For independent Rex/client integration, retain the opt-in `scripts/probe_sites.py --rex-bin …` checks and actual client playback. Those are separate from this provider-only suite.
