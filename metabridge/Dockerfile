# MetaBridge Platform — self-hosted deployment image
FROM python:3.11-slim

RUN useradd --create-home --uid 10001 metabridge
WORKDIR /opt/metabridge

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
RUN pip install --no-cache-dir ".[web,dtd]"

# state (job history, uploads, outputs) lives here — mount a volume
ENV METABRIDGE_DATA_DIR=/data
RUN mkdir -p /data && chown metabridge:metabridge /data
VOLUME /data

# set METABRIDGE_API_KEY at runtime to require authentication on /api
EXPOSE 8000
USER metabridge

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/info')" || exit 1

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
