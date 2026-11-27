FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates libicu-dev libfontconfig1 tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# TwitchDownloaderCLI (MIT) for Twitch chat logs and VOD segments.
# Pin a release with --build-arg TWITCHDOWNLOADER_TAG=<tag>; default is the latest release.
ARG TWITCHDOWNLOADER_TAG=latest
RUN set -eux; \
    api="https://api.github.com/repos/lay295/TwitchDownloader/releases/${TWITCHDOWNLOADER_TAG}"; \
    [ "$TWITCHDOWNLOADER_TAG" = latest ] || api="https://api.github.com/repos/lay295/TwitchDownloader/releases/tags/${TWITCHDOWNLOADER_TAG}"; \
    url=$(curl -fsSL "$api" | python3 -c "import json,sys; print(next(a['browser_download_url'] for a in json.load(sys.stdin)['assets'] if 'CLI' in a['name'] and 'Linux-x64' in a['name']))"); \
    curl -fsSL "$url" -o /tmp/tdcli.zip; \
    unzip /tmp/tdcli.zip -d /usr/local/bin; \
    chmod +x /usr/local/bin/TwitchDownloaderCLI; \
    rm /tmp/tdcli.zip

WORKDIR /app
COPY pyproject.toml README.md ./
COPY clipz ./clipz
RUN pip install --no-cache-dir -e .
COPY config ./config

ENV CLIPZ_DB_PATH=/app/data/clipz.db
VOLUME ["/app/data"]
CMD ["python", "-m", "clipz.pipeline", "--help"]
