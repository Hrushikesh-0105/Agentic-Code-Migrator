# Migrated side: Python 3.11 (same minor version as RepoTransBench's environment).
FROM python:3.11-slim
# pytest-mock / pytest-asyncio: test plugins RepoTransBench projects use without declaring them
# uv: per-project dependency installs pinned to a snapshot date (--exclude-newer)
RUN pip install --no-cache-dir "pytest==8.3.3" "coverage==7.6.1" "pytest-cov==5.0.0" \
      "pytest-mock==3.14.0" "pytest-asyncio==0.24.0" "uv==0.12.18" \
 && useradd -m -u 1000 sandbox
USER sandbox
ENV PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
