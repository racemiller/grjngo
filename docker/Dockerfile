FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges; deno is the recommended one.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN pip install --no-cache-dir requests rapidfuzz "yt-dlp[default]"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/opt/grjngo \
    PYTHONUNBUFFERED=1 \
    GRJNGO_CONFIG=/opt/grjngo/config.toml \
    XDG_CACHE_HOME=/opt/grjngo/data/cache

ENTRYPOINT ["/entrypoint.sh"]
CMD ["status"]
