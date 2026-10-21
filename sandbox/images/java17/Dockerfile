# Python->Java target side. Derived from RepoTransBench's docker/Dockerfile (ubuntu:22.04 + OpenJDK 17 +
# Maven + Python 3.11), trimmed to this regime. Fixes vs upstream: no hardcoded amd64 JAVA_HOME, `tree`
# installed, and a pre-warmed ~/.m2 so `mvn -o test` works inside a --network none sandbox.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries \
 && apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jdk-headless ca-certificates \
 # installed separately so JDK 17 satisfies maven's java dependency (otherwise apt adds OpenJDK 11)
 && apt-get install -y --no-install-recommends maven python3.11 tree \
 && ln -sf /usr/bin/python3.11 /usr/bin/python \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 sandbox
USER sandbox
# Keep warmup/pom.xml in sync with the skeleton pom Layer 0 emits for translated projects.
COPY --chown=sandbox:sandbox warmup/ /home/sandbox/warmup/
RUN cd /home/sandbox/warmup && mvn -B -q test && rm -rf /home/sandbox/warmup
WORKDIR /work
