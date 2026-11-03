FROM python:3.12-slim
LABEL org.opencontainers.image.title="nasrelay" \
      org.opencontainers.image.description="NAS-hosted FTP, SFTP and WebDAV file transfers with Telegram notifications" \
      org.opencontainers.image.source="https://github.com/MaratangNearl/nasrelay" \
      org.opencontainers.image.version="1.0.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data NAS_ROOT=/nas
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 1000 relay \
    && useradd --uid 1000 --gid relay --no-create-home relay \
    && mkdir /data /nas \
    && chown relay:relay /data /nas
COPY --chown=relay:relay app ./app
USER relay
EXPOSE 8080
VOLUME ["/data", "/nas"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-proxy-headers", "--timeout-graceful-shutdown", "75"]
