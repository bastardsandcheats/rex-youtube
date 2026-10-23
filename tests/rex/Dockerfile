# syntax=docker/dockerfile:1
FROM rust:1-trixie
WORKDIR /build
# Supply an unmodified upstream Rex checkout as the named build context.
COPY --from=rexsource /rust/ ./rust/
COPY --from=rexsource /data/ ./data/
COPY --from=rexsource /tests/ ./tests/
COPY patches/rex-manifest-mime.patch /tmp/rex-manifest-mime.patch
RUN git apply --check /tmp/rex-manifest-mime.patch && git apply /tmp/rex-manifest-mime.patch
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/rust/target \
    CARGO_BUILD_JOBS=2 cargo test --locked --release --lib --manifest-path rust/Cargo.toml source_manifest_mime_is_preserved -- --nocapture \
    && cargo test --locked --release --lib --manifest-path rust/Cargo.toml runtime::universal::tests -- --nocapture
