#!/bin/sh
set -eu
exec gunicorn config.events:application --worker-class asgi --workers 1 --bind 0.0.0.0:8000
