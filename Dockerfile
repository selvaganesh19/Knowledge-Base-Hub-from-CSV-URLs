# Knowledge Base Hub - application image.
#
# Two things make this image slightly unusual:
#
#  * PyTorch is installed from the CPU-only index before the rest of the
#    requirements. The default wheel pulls the CUDA build, which is several GB of
#    GPU runtime this application never touches.
#  * The embedding model is downloaded at build time. Otherwise the first search
#    after every container start would pause for a 90 MB download.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libxml2/libxslt back lxml and trafilatura; curl is used by the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not invalidate the dependency layer.
COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# Embedding model cache, baked into the image and pointed at via
# KBHUB_MODEL_CACHE_DIR so it stays out of the data volume.
ARG EMBEDDING_MODEL=all-MiniLM-L6-v2
ENV KBHUB_MODEL_CACHE_DIR=/opt/models
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('${EMBEDDING_MODEL}', cache_folder='/opt/models'); \
print('cached ${EMBEDDING_MODEL}')"

COPY app ./app
COPY run.py ./

# Mutable state lives on a volume: database, vector index, uploads and logs.
# /opt/models is left owned by root - it is read-only at runtime, and chowning it
# would duplicate the whole model cache into another image layer.
ENV KBHUB_DATA_DIR=/data
RUN mkdir -p /data \
    && useradd --create-home --uid 10001 kbhub \
    && chown -R kbhub:kbhub /data /app
USER kbhub
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# A single worker is deliberate: the FAISS index and the background-job registry
# are per-process state. See the deployment section of the README.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
