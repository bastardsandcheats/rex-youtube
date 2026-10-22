# Upstream Rex playback checks

Supply a clean, unmodified Rex checkout:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

This runs Rex's native universal playback unit tests. It applies no patches and does not deploy an image or access the user's libraries. Tested with upstream commit `71570e16f2a74631ee90891f47942b067cf5a93f`.

For actual media decoding, run Rex's `tests/test_native_transcode.py` with `REX_SERVER` and `REX_TRANSCODER` set to built Rex and FFmpeg binaries. This starts disposable fixtures. The main README records its results and limitations. `scripts/probe_sites.py --rex-bin /path/to/rex-server` additionally checks real public-source relays and decoded playback in an isolated catalog.
