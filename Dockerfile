FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data

WORKDIR /app

COPY pyproject.toml setup.py README.md ./
COPY cli_anything ./cli_anything

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && mkdir -p /data/.config/cli-anything-alexa

VOLUME ["/data"]
EXPOSE 3001

ENTRYPOINT ["cli-anything-alexa"]
CMD ["--help"]
