FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 hub \
    && mkdir -p /data \
    && chown hub:hub /data
WORKDIR /app
COPY --chown=10001:10001 . /app/task_hub/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HUB_HOST=0.0.0.0 HUB_DATA_DIR=/data HUB_PORT=8765
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8765/api/health',headers={'Authorization':'Bearer '+os.environ.get('HUB_AUTH_TOKEN','')}); urllib.request.urlopen(r,timeout=3).read()"
CMD ["python", "-m", "task_hub"]
