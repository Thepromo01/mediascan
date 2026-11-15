# MediaScan server (dashboard + API). Build from the project root:
#   docker build -t mediascan .
FROM python:3.12.16-slim-trixie
LABEL org.opencontainers.image.title="MediaScan" \
      org.opencontainers.image.description="Shrink a movie/TV library to HEVC with VMAF-verified quality, and fill in missing subtitles" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.version="1.0.0"

# ffmpeg/ffprobe: scanning the library and the final check before a file is replaced
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app ./app
COPY worker ./worker-src/worker
COPY tools ./worker-src/tools
# worker install packages + the files workers fetch when they update themselves
RUN mkdir -p worker-src/server/app && cp app/langs.py worker-src/server/app/ \
 && python worker-src/tools/build_packages.py /srv/dist && rm -rf worker-src

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# trust X-Forwarded-* only from a reverse proxy on the host itself (it arrives via the Docker network)
ENV MEDIA_ROOT=/media DB_PATH=/data/mediascan.db DIST_DIR=/srv/dist TZ=UTC \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FORWARDED_ALLOW_IPS=127.0.0.1,172.16.0.0/12
EXPOSE 8780
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8780/api/health')"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8780", "--workers", "1"]
