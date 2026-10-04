FROM python:3.12-slim AS bot

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data POT_BASE_URL=http://127.0.0.1:4416

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Node.js >=22 for yt-dlp JS runtime and the bgutil POT provider.
COPY --from=node:22-bookworm-slim /usr/local /usr/local
# Deno: yt-dlp's default JS runtime (must be on PATH).
COPY --from=denoland/deno:latest /usr/bin/deno /usr/local/bin/deno
# Prebuilt bgutil server (build + node_modules) from the official image.
COPY --from=brainicism/bgutil-ytdlp-pot-provider:latest /app /opt/bgutil

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U yt-dlp

COPY . .

RUN useradd -m -u 1000 bot && mkdir -p /app/data && chown -R bot:bot /app /opt/bgutil
USER bot

EXPOSE 4416
CMD ["sh", "-c", "node /opt/bgutil/build/main.js --host 0.0.0.0 & exec python bot.py"]
