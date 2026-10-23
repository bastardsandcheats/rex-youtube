# Generated reports

Run `venv/bin/python scripts/check_providers.py --all` to regenerate the provider suite. Use `--configured` to test enabled sources and inherited settings from the private YAML, or supply an arbitrary public URL to check it without registration or a settings change.

- [PROVIDERS.md](PROVIDERS.md) is generated from provider files, including test URLs, handler descriptions and a handler diagram. `--list` regenerates it without network tests.
- `latest.md` and `latest.json` hold the latest check/run.
- `suite.md` and `suite.json` hold the latest complete-provider run.
- `configured.md` and `configured.json` hold the latest configured-source run.

Run reports are automatically replaced, saved incrementally during suites, and ignored by Git. This avoids accumulating diagnostic logs or recording files. The static provider inventory can be committed and shared. A report's timestamp/ID identifies the run; see [the checking guide](../docs/CHECKING_PROVIDERS.md) for levels, limitations and exit codes.
