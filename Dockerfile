FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir . -r requirements-mcp.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/edge-tts-audio \
    && chown appuser:appuser /tmp/edge-tts-audio
USER appuser
EXPOSE 8000
CMD ["python", "railway_mcp.py"]
