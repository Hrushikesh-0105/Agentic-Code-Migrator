# C and C++ (legacy C / C++ sources; C++ targets). GCC 11 + CMake on Ubuntu 22.04, as in RepoTransBench.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 sandbox
USER sandbox
WORKDIR /work
