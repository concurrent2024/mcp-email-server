# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EMAIL_ATTACHMENT_DIR=/data/attachments

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /data/attachments \
    && chown -R app:app /data

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && chown -R app:app /app

USER app

EXPOSE 8000

# HTTP mode binds 0.0.0.0 so published ports work. Override the command
# for --check or stdio. Pass mail secrets at runtime via --env-file, not here.
CMD ["mcp-email-server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
