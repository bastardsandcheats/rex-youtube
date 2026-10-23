# Upstream Rex playback checks

Supply a clean, unmodified Rex checkout:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

This applies only the included manifest MIME patch, runs its regression check and Rex’s native universal playback unit tests, and does not deploy an image or access the user’s libraries. The isolated unit-test build was verified against upstream `a9d5eedc7a7fe39e8b0b1693547d3d9b1300b0c4`: the MIME check and 20 universal playback checks passed, with one upstream check ignored. The later browser walkthrough uses `3bdf52bcb2e13ad414b06133768b15d6a753b1fc`; see the main README for the separate playback results.

For actual media decoding, run Rex's `tests/test_native_transcode.py` with `REX_SERVER` and `REX_TRANSCODER` set to built Rex and FFmpeg binaries. This starts disposable fixtures. The main README records its results and limitations. `scripts/probe_sites.py --rex-bin /path/to/rex-server` additionally checks real public-source relays and decoded playback in an isolated catalog.
