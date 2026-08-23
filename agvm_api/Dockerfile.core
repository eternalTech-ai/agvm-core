# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGVM_EDITION=core \
    AGVM_ALLOW_SETUP_WITHOUT_PROVIDER=true \
    PYTHONPATH=/app:/app/sdk/python

WORKDIR /app
COPY agvm_api/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY agvm_api .
COPY sdk/python ./sdk/python
COPY agvm_mcp_server ./agvm_mcp_server
RUN groupadd --system agvm \
    && useradd --system --gid agvm --home-dir /app --shell /usr/sbin/nologin agvm \
    && mkdir -p /app/data /app/brains \
    && chown -R agvm:agvm /app

USER agvm

EXPOSE 8010
CMD ["uvicorn", "core_api_app:app", "--host", "0.0.0.0", "--port", "8010"]
