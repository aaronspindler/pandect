# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# Pinned scanner-only targets. They never receive application credentials/data.
FROM ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f AS secret-scanner
FROM ghcr.io/aquasecurity/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa AS vulnerability-scanner

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY manage.py LICENSE NOTICE ./
COPY pandect ./pandect
COPY static ./static
COPY templates ./templates
COPY scripts ./scripts
RUN chmod -R a+rX manage.py pyproject.toml uv.lock LICENSE NOTICE pandect static templates scripts \
    && PANDECT_DATA_DIR=/tmp/pandect-build \
    PANDECT_DEBUG=false \
    PANDECT_SECRET_KEY=container-image-build-only \
    .venv/bin/python manage.py collectstatic --noinput

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS runtime

ENV HOME=/tmp \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    OMP_THREAD_LIMIT=1 \
    OPENBLAS_NUM_THREADS=1 \
    PATH=/app/.venv/bin:$PATH \
    PANDECT_DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ffmpeg \
        libgomp1 \
        libpcre2-8-0 \
        libseccomp2 \
        libimage-exiftool-perl \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 pandect \
    && useradd --uid 10001 --gid pandect --no-create-home \
        --shell /usr/sbin/nologin pandect \
    && mkdir -p /data \
    && chown pandect:pandect /data

WORKDIR /app

# Keep the large immutable native/Python environment in its own cached layer.
# Application code is root-owned and world-readable; runtime users cannot edit it.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/manage.py /app/pyproject.toml /app/uv.lock /app/LICENSE /app/NOTICE ./
COPY --from=builder /app/pandect ./pandect
COPY --from=builder /app/static ./static
COPY --from=builder /app/staticfiles ./staticfiles
COPY --from=builder /app/templates ./templates
COPY --from=builder /app/scripts ./scripts

USER pandect

EXPOSE 8000

CMD ["gunicorn", "pandect.config.wsgi:application", "--bind=0.0.0.0:8000", "--workers=1", "--threads=4", "--timeout=60", "--graceful-timeout=30", "--access-logfile=-", "--error-logfile=-"]

FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS tools

ENV HOME=/tmp \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    OMP_THREAD_LIMIT=1 \
    OPENBLAS_NUM_THREADS=1 \
    PATH=/opt/pandect-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/pandect-venv \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        ffmpeg \
        libgomp1 \
        libpcre2-8-0 \
        libseccomp2 \
        libimage-exiftool-perl \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 pandect \
    && useradd --uid 10001 --gid pandect --no-create-home \
        --shell /usr/sbin/nologin pandect

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

USER pandect

CMD ["python", "manage.py", "check"]

# Setup/recovery must work before the application or its dependencies are built.
# The maintenance helper is standard-library-only and never imports Django.
FROM python:3.13-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64 AS maintenance

# Refresh inherited PCRE2 and Perl from Debian security (DLA-4772-1/DLA-4821-1).
# Runtime/tools install PCRE2 and Perl-dependent ExifTool above.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes libpcre2-8-0 perl-base \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY LICENSE NOTICE ./
COPY scripts ./scripts
COPY pandect/config/data_paths.py ./pandect/config/data_paths.py
RUN chmod -R a+rX /app/scripts /app/pandect /app/LICENSE /app/NOTICE
CMD ["python", "scripts/maintenance.py", "--help"]
