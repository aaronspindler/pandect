FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.14.9-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/pandect-venv \
    UV_LINK_MODE=copy \
    PATH="/opt/pandect-venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

RUN groupadd --system pandect && useradd --system --create-home --gid pandect pandect \
    && mkdir -p /app/data && chown pandect:pandect /app/data
COPY manage.py ./
COPY config/ config/
COPY accounts/ accounts/
COPY cases/ cases/
COPY core/ core/
COPY people/ people/
COPY vehicle/ vehicle/
COPY text/ text/
COPY image/ image/
COPY audio/ audio/
COPY document/ document/
COPY geo/ geo/
COPY phone/ phone/
COPY socialmedia/ socialmedia/
COPY video/ video/
COPY marketing/ marketing/
COPY ui/ ui/
COPY scripts/docker-entrypoint-worker.sh scripts/docker-entrypoint-worker.sh
# A synthetic build-only setting is sufficient; collectstatic never needs a DB.
RUN DJANGO_SECRET_KEY=static-collection-build-only python manage.py collectstatic --noinput \
    && chown -R pandect:pandect /app/staticfiles
ARG APP_COMMIT_SHA=unknown
ENV APP_COMMIT_SHA=${APP_COMMIT_SHA}
LABEL org.opencontainers.image.revision=${APP_COMMIT_SHA}
USER pandect
EXPOSE 8000

FROM runtime AS worker
CMD ["sh", "/app/scripts/docker-entrypoint-worker.sh"]

FROM runtime AS web
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--timeout", "180", "--access-logfile", "-", "--logger-class", "config.privacy.PrivateAccessLogger"]
