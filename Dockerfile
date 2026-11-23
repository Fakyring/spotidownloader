FROM node:24-bookworm-slim AS node-runtime

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
RUN node --version && ffmpeg -version && ffprobe -version

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt 'waitress>=3.0.2,<4'

COPY app.py ./
COPY templates/ ./templates/
COPY static/ ./static/

USER 10001:10001
EXPOSE 8765

# Selection tokens and download progress are shared by threads in one process.
CMD ["waitress-serve", "--listen=0.0.0.0:8765", "--threads=8", "app:app"]
