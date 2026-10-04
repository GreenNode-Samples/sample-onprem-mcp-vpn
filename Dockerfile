FROM python:3.12-slim

# Run as an unprivileged user; the SQLite database lives in /data (mount a volume to persist it).
RUN useradd --system --uid 10001 --create-home --home-dir /home/mcp mcp \
    && mkdir -p /app /data \
    && chown -R mcp:mcp /app /data

WORKDIR /app
# Build context: repo root (docker build -t onprem-mcp-server .)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/onprem_mcp/main.py src/onprem_mcp/healthcheck.py ./

ENV PORT=8080 \
    DB_PATH=/data/erp.db \
    PYTHONUNBUFFERED=1

USER mcp
VOLUME ["/data"]
EXPOSE 8080

# Probes the address the server is bound to (HOST, default 0.0.0.0 -> 127.0.0.1) and PORT; also used by docker compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "main.py"]
