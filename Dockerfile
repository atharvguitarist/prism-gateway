FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY prism ./prism
COPY data ./data
COPY config ./config
COPY scripts ./scripts

# non-root; /data holds the SQLite file (mounted as a named volume by compose so it survives restarts)
RUN useradd --system --uid 10001 prism && mkdir /data && chown prism /data
USER prism

ENV PRISM_DATA_DIR=/app/data \
    PRISM_CONFIG=/app/config/gateway_config.docker.json \
    PRISM_DB=/data/prism.sqlite3

EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"

# ONE worker on purpose: rate-limit state lives in process memory (see README "Known limitations").
CMD ["uvicorn", "prism.app:app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
