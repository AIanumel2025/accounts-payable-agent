# FastAPI review/operations API (M11E). Private service: it is never exposed
# to the browser; only the Next.js web service calls it.
#
# Multi-stage: dependencies are built into a virtualenv in the builder stage;
# the runtime image carries only the venv and the application source, runs as
# a non-root user and contains no credentials (all configuration arrives
# through environment variables at run time).
#
# Build:  docker build -f docker/api.Dockerfile -t ap-agent-api .

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ src/
# api: FastAPI, uvicorn, PyJWT[crypto]; postgres: psycopg; s3: boto3 (Cloudflare R2).
RUN pip install ".[api,postgres,s3]"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Migration runner, role helper and administrative CLI used by the pre-deploy step.
COPY scripts/ scripts/
COPY src/ src/

RUN useradd --create-home --shell /usr/sbin/nologin ap_agent
USER ap_agent

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health/live' % os.environ.get('PORT','8000'), timeout=3)"]

# Bounded keep-alive and graceful shutdown (in-flight requests get 20 s to finish on SIGTERM).
CMD ["sh", "-c", "exec uvicorn ap_agent.api.app:create_app --factory --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 5 --timeout-graceful-shutdown 20 --log-level info --no-server-header"]
