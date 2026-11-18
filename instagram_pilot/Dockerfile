FROM python:3.14.9-slim-trixie
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir playwright==1.65.1 greenlet==3.5.6 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 pilot
WORKDIR /pilot
COPY collector.py server.py ./
USER 1000
EXPOSE 8080
CMD ["python", "server.py"]
