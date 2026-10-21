# Rust targets (Python->Rust, C->Rust). Stable Rust as in RepoTransBench; the standard library only (runs are
# offline, crates.io is unreachable).
FROM rust:1-slim-bookworm
RUN useradd -m -u 1000 sandbox
USER sandbox
ENV CARGO_NET_OFFLINE=true CARGO_TARGET_DIR=/tmp/target CARGO_INCREMENTAL=0 CARGO_TERM_COLOR=never
RUN rustc --version && cargo --version
WORKDIR /work
