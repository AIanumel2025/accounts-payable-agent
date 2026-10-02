# FastAPI review/operations API as an AWS Lambda container (M11E.1).
#
# The AWS Lambda Web Adapter (an extension) translates Function URL invocations into plain HTTP calls to
# uvicorn on $AWS_LWA_PORT, so the application code is unchanged. The Function URL uses AWS_IAM auth: only
# the web function's execution role can invoke it (see deploy/aws/template.yaml). Secrets are fetched from
# SSM Parameter Store at start-up by the application (ap_agent.aws.secrets); nothing secret is in this image.
#
# Build (from the repository root):
#   docker build -f docker/api.lambda.Dockerfile -t ap-agent-api-lambda .

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install ".[api,postgres,s3]"

FROM python:3.11-slim AS runtime

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/health/live \
    AWS_LWA_INVOKE_MODE=buffered

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src/ src/

CMD ["sh", "-c", "exec uvicorn ap_agent.api.app:create_app --factory --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 5 --timeout-graceful-shutdown 20 --log-level info --no-server-header"]
