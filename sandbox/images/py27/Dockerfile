# Legacy side: real CPython 2.7 for running un-migrated code.
FROM python:2.7.18-slim-buster
# buster's apt mirrors are archived, so avoid apt-get; pytest 4.6 is the last py2 release
RUN pip install --no-cache-dir "pytest==4.6.11" "mock==3.0.5" && useradd -m -u 1000 sandbox
USER sandbox
ENV PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8
WORKDIR /work
