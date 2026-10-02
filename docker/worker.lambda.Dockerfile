# Invoice worker as an AWS Lambda container image (M11E.1), triggered by SQS FIFO.
#
# Same application code, dependency pins (paddlepaddle 3.3.1 / paddleocr 3.7.0 / pymupdf 1.28.2) and Debian
# python:3.11 base as docker/worker.Dockerfile -- i.e. the OCR runtime the validated pipeline already uses --
# plus the AWS Lambda Runtime Interface Client so it runs as a Lambda container (handler:
# ap_agent.worker.lambda_handler.handler).
#
# The PaddleOCR model weights are downloaded into the image at BUILD time (to /opt/paddlex) and are read-only
# at run time: a cold start never depends on a model host being reachable. HOME and every cache directory are
# redirected to /tmp (the only writable path on Lambda). Real PaddleOCR only; no substitution.
#
# Memory/CPU: Lambda allocates CPU in proportion to memory. Start at 4096 MB, raise it if the benchmark
# (docs/m11e1_aws_go_live_report.md) misses the 12-minute target.
#
# Build:  docker build -f docker/worker.lambda.Dockerfile -t ap-agent-worker-lambda .

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install ".[postgres,s3,pdf,preprocessing,ocr-paddle,ocr-tesseract]" "awslambdaric==4.1.0"

FROM python:3.11-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mpl \
    PADDLE_PDX_CACHE_HOME=/tmp/paddlex \
    AP_AGENT_PADDLEX_BAKED_DIR=/opt/paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    OMP_NUM_THREADS=2 \
    AP_AGENT_ENVIRONMENT=hosted \
    AP_AGENT_ENABLE_WORKER_EXECUTION=true \
    AP_AGENT_WORKER_OCR_PROVIDER=paddleocr \
    AP_AGENT_ARTIFACT_STORAGE=aws_s3 \
    AP_AGENT_REFERENCE_DATA_DIRECTORY=/opt/ap-agent/reference-data \
    AP_AGENT_PHASE_ARTIFACT_ROOT=/tmp/ap-agent-phases

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src/ src/
# MVP reference data (suppliers, purchase orders, goods receipts) shipped with the image (see D-5 in
# docs/m11e_hosted_deployment_report.md). Replace it with the organisation's own master data before real use.
COPY tests/fixtures/reference_data/ /opt/ap-agent/reference-data/

# Download the OCR model weights AND the fonts PaddleX fetches on first use now (build time) into /opt/paddlex, and
# make them world-readable (Lambda runs the function under an unpredictable non-root uid). At run time
# `ap_agent.aws.paddle_cache.prepare_paddlex_cache` builds the writable /tmp/paddlex from symlinks to them, so a cold
# start never downloads anything.
RUN mkdir -p /opt/paddlex
RUN PADDLE_PDX_CACHE_HOME=/opt/paddlex python - <<'PY'
from ap_agent.adapters.paddleocr_adapter import create_engine
from ap_agent.config.settings import PaddleEngineOptions

create_engine(PaddleEngineOptions())
from paddlex.utils.fonts import PINGFANG_FONT, SIMFANG_FONT

print("fonts:", PINGFANG_FONT.path, SIMFANG_FONT.path)
print("PaddleOCR models and fonts cached.")
PY
RUN chmod -R a+rX /opt/paddlex /opt/ap-agent /app /opt/venv

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ap_agent.worker.lambda_handler.handler"]
