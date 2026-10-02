# Migration / administrative task as an AWS Lambda container (M11E.1). Has NO Function URL: it is invoked only
# deliberately (`aws lambda invoke`). It is the only function that receives the migration/owner database DSN.
#
# Build:  docker build -f docker/migrate.lambda.Dockerfile -t ap-agent-migrate-lambda .

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install ".[postgres,s3]" "awslambdaric==4.1.0"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src/ src/
COPY scripts/migrate.py scripts/manage_identity_mappings.py scripts/

RUN chmod -R a+rX /app /opt/venv

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ap_agent.aws.migration_handler.handler"]
