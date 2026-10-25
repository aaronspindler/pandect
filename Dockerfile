FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.14.9-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/pandect-venv \
    UV_LINK_MODE=copy \
    PATH="/opt/pandect-venv/bin:$PATH"

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

RUN groupadd --system pandect && useradd --system --create-home --gid pandect pandect \
    && mkdir -p /app/data && chown pandect:pandect /app/data
COPY manage.py ./
COPY config/ config/
COPY accounts/ accounts/
USER pandect
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--access-logfile", "-"]
