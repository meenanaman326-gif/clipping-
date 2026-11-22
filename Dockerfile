# Deno is used by yt-dlp for YouTube's JavaScript player challenge solver.
FROM denoland/deno:bin AS deno

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && deno --version
COPY app ./app
ENV PORT=10000 MAX_VIDEO_SECONDS=1500 MAX_UPLOAD_MB=200 CLIP_COUNT=5
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
