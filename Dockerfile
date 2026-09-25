# Accounts Payable Agent -- application image (M8 task §9).
#
# Packages application code and the Phase 7 migration files only.
# Does NOT package:
#   - database files or PostgreSQL data (production PostgreSQL stays
#     external to this container; the Compose `postgres` service is
#     development/test infrastructure only -- see docker-compose.yml);
#   - generated invoice artifacts (`tests/fixtures/invoices/`,
#     `notebooks/`, and any runtime `artifact_root` output are excluded
#     via .dockerignore);
#   - `.env` (excluded via .dockerignore; credentials are injected at
#     container run time through environment variables, never baked in).

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY scripts/ scripts/

RUN pip install --no-cache-dir ".[postgres]"

# Non-root application user (task §9: "Use a non-root application user.").
RUN useradd --create-home --shell /usr/sbin/nologin ap_agent
USER ap_agent

# No default long-running command: M8 packages the library, migration
# runner and smoke-test script only. Orchestration/API entry points are
# out of scope for this milestone (task: "Do not begin orchestration,
# LLM reasoning, FastAPI, UI development"). `docker compose run` selects
# the command explicitly (see docker-compose.yml's `migrate` and
# `memory-smoke-test` services).
CMD ["python", "-c", "import ap_agent; print('ap_agent image ready — see docker-compose.yml for migrate/memory-smoke-test commands')"]
