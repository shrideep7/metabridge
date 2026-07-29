FROM python:3.11-slim

RUN useradd --create-home --uid 10001 metabridge
WORKDIR /opt/metabridge

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web

RUN pip install --no-cache-dir ".[web,dtd,connectors]"

ENV METABRIDGE_DATA_DIR=/data

RUN mkdir -p /data && chown metabridge:metabridge /data

VOLUME /data

EXPOSE 8000

USER metabridge

CMD ["uvicorn","web.app:app","--host","0.0.0.0","--port","8000","--workers","2"]