# syntax=docker/dockerfile:1
FROM rust:1-trixie
WORKDIR /build
# Supply a clean Rex checkout as the rexsource build context.
COPY --from=rexsource /rust/ ./rust/
COPY --from=rexsource /data/ ./data/
COPY --from=rexsource /tests/ ./tests/
# YouTube requires only the HLS feed/playback patch included in this example.
COPY patches/rex-hls-vod.patch /tmp/rex-hls-vod.patch
RUN git apply --check /tmp/rex-hls-vod.patch && git apply /tmp/rex-hls-vod.patch
COPY tests/rex/add-hls-tests.py /tmp/add-hls-tests.py
RUN python3 /tmp/add-hls-tests.py
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/rust/target \
    CARGO_BUILD_JOBS=2 cargo test --locked --release --lib --manifest-path rust/Cargo.toml local_hls -- --nocapture \
    && cargo test --locked --release --lib --manifest-path rust/Cargo.toml runtime::universal::tests -- --nocapture
