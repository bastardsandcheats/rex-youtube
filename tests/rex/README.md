# Rex HLS regression checks

These checks apply only `patches/rex-hls-vod.patch` to a clean Rex checkout. They test HLS feed acceptance, playlist MIME type, H.264/AAC MP4 codec tags, seeking, and Rex's universal playback tests.

From this project's root, supply your Rex source checkout:

```sh
docker build --build-context rexsource=/path/to/rex-source -f tests/rex/Dockerfile .
```

The tests do not require a running Rex instance, account credentials, configuration, or any other Rex patches. They do not deploy the resulting image.
