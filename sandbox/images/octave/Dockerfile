# Matlab legacy sources (Matlab->Python) run on GNU Octave (as RepoTransBench's Matlab projects do): 7.3 in
# Debian bookworm, which has jsonencode / jsondecode.
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends octave \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 sandbox
USER sandbox
RUN octave --no-gui --no-window-system --eval "disp(jsonencode(struct('a', 1)))"
WORKDIR /work
