# Upstream Rex playback checks

Supply a clean, unmodified Rex checkout:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

This runs Rex's upstream native universal playback unit tests without applying local patches. It does not deploy an image or access your libraries.

The earlier recipe was verified against `a9d5eedc7a7fe39e8b0b1693547d3d9b1300b0c4` with the former MIME patch: its MIME regression and 20 universal playback checks passed, with one upstream check ignored. The patch-free recipe has not been rebuilt as part of the latest fast checks. See the main README and test findings for the separately verified patch-free playback results against `306f4aa992700f6a3df5eabd5eca731d24dbedf5`.

For actual media decoding, run Rex's `tests/test_native_transcode.py` with `REX_SERVER` and `REX_TRANSCODER` set to built Rex and FFmpeg binaries. This starts disposable fixtures. `scripts/probe_sites.py --rex-bin /path/to/rex-server` additionally checks real public-source relays and decoded playback in an isolated catalog.
