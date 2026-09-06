# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

WORKDIR /app
COPY agvm_api/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Release metadata changes for every coherent bundle. Keep it after the
# expensive OS/Python dependency layers so a new source SHA does not force a
# full dependency rebuild.
ARG AGVM_IMAGE_VERSION=0.0.0-dev
ARG AGVM_IMAGE_REVISION=unknown
ARG AGVM_IMAGE_CREATED=1970-01-01T00:00:00Z
ARG AGVM_RELEASE_SOURCE_SHA=
ARG AGVM_RELEASE_BUNDLE_ID=
ARG AGVM_RELEASE_BUNDLE_MANIFEST_JSON=

LABEL org.opencontainers.image.title="Detwin Core API" \
      org.opencontainers.image.version="${AGVM_IMAGE_VERSION}" \
      org.opencontainers.image.revision="${AGVM_IMAGE_REVISION}" \
      org.opencontainers.image.created="${AGVM_IMAGE_CREATED}" \
      org.opencontainers.image.vendor="Eternal Tech SRL" \
      ai.detwin.release.source_sha="${AGVM_RELEASE_SOURCE_SHA}" \
      ai.detwin.release.bundle="${AGVM_RELEASE_BUNDLE_ID}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGVM_EDITION=core \
    AGVM_ALLOW_SETUP_WITHOUT_PROVIDER=true \
    AGVM_IMAGE_VERSION=${AGVM_IMAGE_VERSION} \
    AGVM_IMAGE_REVISION=${AGVM_IMAGE_REVISION} \
    AGVM_RELEASE_SOURCE_SHA=${AGVM_RELEASE_SOURCE_SHA} \
    AGVM_RELEASE_BUNDLE_ID=${AGVM_RELEASE_BUNDLE_ID} \
    AGVM_RELEASE_BUNDLE_MANIFEST_JSON=${AGVM_RELEASE_BUNDLE_MANIFEST_JSON} \
    PYTHONPATH=/app:/app/sdk/python

COPY agvm_api .
COPY sdk/python ./sdk/python
COPY agvm_mcp_server ./agvm_mcp_server
# Search and Grow load these normative metamemory documents at runtime. Keep
# them in the same /app/docs location used by source checkouts so packaged Core
# images validate the exact same spatial contract instead of failing closed.
COPY docs/AGVM_MASTER.md ./docs/AGVM_MASTER.md
COPY docs/AGVM_METAMEMORY_SPATIAL_BRIEF.md ./docs/AGVM_METAMEMORY_SPATIAL_BRIEF.md
RUN groupadd --system --gid 10001 agvm \
    && useradd --system --uid 10001 --gid agvm --home-dir /app --shell /usr/sbin/nologin agvm \
    && mkdir -p /app/data /app/brains \
    && chown -R agvm:agvm /app

USER agvm

EXPOSE 8010
CMD ["uvicorn", "core_api_app:app", "--host", "0.0.0.0", "--port", "8010"]
