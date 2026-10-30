FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.14.9-slim-trixie

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
COPY background/ background/
COPY accounts/ accounts/
COPY cases/ cases/
COPY evidence/ evidence/
COPY analysis/ analysis/
COPY people/ people/
COPY vehicles/ vehicles/
COPY templates/ templates/
COPY static/ static/
# A synthetic build-only setting is sufficient; collectstatic never needs a DB.
RUN DJANGO_SECRET_KEY=static-collection-build-only python manage.py collectstatic --noinput \
    && chown -R pandect:pandect /app/staticfiles
USER pandect
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--timeout", "180", "--access-logfile", "-", "--logger-class", "config.privacy.PrivateAccessLogger"]
