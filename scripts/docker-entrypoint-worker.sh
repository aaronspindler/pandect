#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py prepare_case_search
printf 'PANDECT_WORKER_PREPARED %s\n' "$APP_COMMIT_SHA"
exec python manage.py db_worker --no-reload
