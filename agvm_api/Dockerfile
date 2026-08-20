FROM python:3.11-slim

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

COPY sdk/python ./sdk/python
RUN pip install --no-cache-dir ./sdk/python

COPY agvm_api .
COPY agvm_mcp_server ./agvm_mcp_server
RUN mkdir -p /app/data /app/brains

EXPOSE 8010
CMD ["uvicorn", "core_api_app:app", "--host", "0.0.0.0", "--port", "8010"]
