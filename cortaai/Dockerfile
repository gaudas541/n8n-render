# Node 22 supports yt-dlp's current YouTube extraction challenge runtime.
FROM node:22-bookworm-slim AS nodebin
FROM python:3.12-slim-bookworm
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py video_links.py ./
COPY static ./static
RUN mkdir -p /tmp/cortaai
ENV CORTAAI_DATA=/tmp/cortaai
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]