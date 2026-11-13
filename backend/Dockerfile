# syntax=docker/dockerfile:1.7
# One image, four roles: `api` (default), `worker`, `scheduler`, `migrate`. See docs/DEPLOYMENT.md.
# Build: docker build -t rag-backend backend

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN /app/.venv/bin/python -m compileall -q app alembic

FROM python:3.12-slim-bookworm
# tesseract for OCR (eng data; add tesseract-ocr-<lang> per OCR_LANGS). No compilers in this stage.
# tini is PID 1: the kernel drops default-action signals sent to PID 1, so uvicorn's re-raised
# SIGTERM would not end it while interpreter exit waits on a busy worker thread.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 rag \
 && useradd --uid 10001 --gid rag --no-create-home --shell /usr/sbin/nologin rag \
 && mkdir -p /models /data /tmp \
 && chown rag:rag /models /data

COPY --from=build /app /app
# Model caches live on a mounted volume (/models) so containers stay stateless and can run read-only.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/models/fastembed \
    HF_HOME=/models/hf \
    TRANSCRIBE_MODEL_DIR=/models/whisper \
    BLOB_DIR=/data/blobs \
    PORT=8000

COPY --chmod=0755 <<'EOF' /usr/local/bin/rag-entrypoint
#!/bin/sh
set -eu
role="${1:-api}"
[ $# -gt 0 ] && shift
case "$role" in
  # In-flight requests (SSE included) get 20 s, inside the 30 s K8s/compose stop grace period.
  api) exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --no-server-header \
         --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
         --timeout-graceful-shutdown 20 "$@" ;;
  worker) exec dramatiq app.worker "$@" ;;
  scheduler) exec python -m app.connectors.scheduler "$@" ;;
  # MIGRATION_DATABASE_URL must be the rag_owner role (ADR 0009), never the superuser.
  migrate) exec alembic upgrade head ;;
  *) exec "$role" "$@" ;;
esac
EOF

WORKDIR /app
USER 10001:10001
EXPOSE 8000
# Only meaningful for the api role; compose/k8s disable or replace it for worker/scheduler/migrate.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'], timeout=2)"]
ENTRYPOINT ["tini", "--", "rag-entrypoint"]
CMD ["api"]
