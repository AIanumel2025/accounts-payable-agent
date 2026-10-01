# OCR worker (M11E): the single M11D worker process, now reading uploads from
# object storage and running the real PaddleOCR provider.
#
# Memory: PaddleOCR (detection + recognition + document unwarping +
# text-line orientation, CPU) holds roughly 2-3 GB resident while processing an
# invoice. Size the Render instance for at least 4 GB; a 2 GB instance will be
# OOM-killed on real invoices. See docs/m11e_deployment_runbook.md.
#
# Model initialisation is predictable: the model weights are downloaded into
# the image at BUILD time (below), so a deploy never depends on a model host
# being reachable at start-up, and the worker builds the engine once at start
# (`python -m ap_agent.worker` in the hosted environment) and exits non-zero
# with a clear message if it cannot.
#
# Build:  docker build -f docker/worker.Dockerfile -t ap-agent-worker .

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ src/
# Versions are pinned in pyproject.toml (paddlepaddle==3.3.1, paddleocr==3.7.0, pymupdf==1.28.2).
RUN pip install ".[postgres,s3,pdf,preprocessing,ocr-paddle,ocr-tesseract]"

FROM python:3.11-slim AS runtime

# Shared libraries OpenCV/Paddle need on a slim base, plus the Tesseract binary
# the Phase 3 fallback router uses when a page needs the secondary provider.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/ap_agent \
    PADDLE_PDX_CACHE_HOME=/home/ap_agent/.paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    AP_AGENT_ENVIRONMENT=hosted \
    AP_AGENT_ENABLE_WORKER_EXECUTION=true \
    AP_AGENT_WORKER_OCR_PROVIDER=paddleocr \
    AP_AGENT_ARTIFACT_STORAGE=s3 \
    AP_AGENT_REFERENCE_DATA_DIRECTORY=/opt/ap-agent/reference-data \
    AP_AGENT_PHASE_ARTIFACT_ROOT=/tmp/ap-agent-phases

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src/ src/
# MVP reference data (suppliers, purchase orders, goods receipts) shipped with the
# image. Replace it with the organisation's own master data before real use.
COPY tests/fixtures/reference_data/ /opt/ap-agent/reference-data/

RUN useradd --create-home --shell /usr/sbin/nologin ap_agent \
    && mkdir -p /tmp/ap-agent-phases \
    && chown -R ap_agent:ap_agent /tmp/ap-agent-phases
USER ap_agent

# Download the OCR model weights now (build time) into the image's PaddleX cache.
RUN python - <<'PY'
from ap_agent.adapters.paddleocr_adapter import create_engine
from ap_agent.config.settings import PaddleEngineOptions

create_engine(PaddleEngineOptions())
print("PaddleOCR models cached.")
PY

# SIGTERM is delivered to this process directly (exec form): the in-flight job
# finishes, then the worker exits.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "ap_agent.worker"]
