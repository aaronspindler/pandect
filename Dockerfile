# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a AS uv

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY manage.py ./
COPY pandect ./pandect
COPY static ./static
COPY templates ./templates
COPY scripts/worker-healthcheck.py ./scripts/worker-healthcheck.py
RUN PANDECT_DATA_DIR=/tmp/pandect-build \
    PANDECT_DEBUG=false \
    PANDECT_SECRET_KEY=container-image-build-only \
    .venv/bin/python manage.py collectstatic --noinput

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS runtime

ENV HOME=/tmp \
    PATH=/app/.venv/bin:$PATH \
    PANDECT_DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 pandect \
    && useradd --uid 10001 --gid pandect --no-create-home \
        --shell /usr/sbin/nologin pandect \
    && mkdir -p /data \
    && chown pandect:pandect /data

WORKDIR /app

COPY --from=builder --chown=pandect:pandect /app /app

USER pandect

EXPOSE 8000

CMD ["gunicorn", "pandect.config.wsgi:application", "--bind=0.0.0.0:8000", "--workers=1", "--threads=4", "--timeout=60", "--graceful-timeout=30", "--access-logfile=-", "--error-logfile=-"]

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS tools

ENV HOME=/tmp \
    PATH=/opt/pandect-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/pandect-venv

COPY --from=uv /uv /uvx /usr/local/bin/

RUN groupadd --gid 10001 pandect \
    && useradd --uid 10001 --gid pandect --no-create-home \
        --shell /usr/sbin/nologin pandect

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

USER pandect

CMD ["python", "manage.py", "check"]
