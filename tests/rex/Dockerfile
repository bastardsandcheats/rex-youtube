# syntax=docker/dockerfile:1
FROM rust:1-trixie
WORKDIR /build
# Supply an unmodified upstream Rex checkout as the named build context.
COPY --from=rexsource /rust/ ./rust/
COPY --from=rexsource /data/ ./data/
COPY --from=rexsource /tests/ ./tests/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/rust/target \
    CARGO_BUILD_JOBS=2 cargo test --locked --release --lib --manifest-path rust/Cargo.toml runtime::universal::tests -- --nocapture
