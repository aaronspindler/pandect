# Background-worker foundation

Pandect uses Django's native Tasks API with `django-tasks-db` 0.13 and its existing
PostgreSQL database. One `default` queue is shared by web and worker processes.
There is no Redis service, scheduler, or external queue. AI integration belongs
to the case-scoped analysis application. The
backend remains asynchronous when `DEBUG=true`.

The synthetic diagnostic returns a nonce; [case analysis](analysis.md) and
[vehicle-location CSV imports](vehicle-location-imports.md) also use this queue
with separate durable case-scoped records, progress, and recovery.
Invitations, evidence retention, and file cleanup keep their existing synchronous
behavior. Other lengthy ingestion must add its own
authorization, idempotency, and recovery before using this foundation.

## Startup and diagnostics

Complete [local setup](development.md), then start the stack:

```sh
docker compose up --build --wait
docker compose exec web python manage.py check_task_worker --timeout 30
docker compose logs --tail 100 worker
```

The worker uses the same image, configuration, and private storage volume as web.
It starts after web becomes healthy, following migrations, and runs
`python manage.py db_worker --no-reload`. Compose restarts unexpected worker exits
and allows 30 seconds for graceful shutdown before forcing termination. Restart
or rebuild the worker after source changes; it does not reload automatically.
For hosted deployments, apply migrations once before starting web and workers.

`check_task_worker` enqueues a fresh random nonce, waits for its matching result,
and prints the task UUID and `OK`. It exits nonzero for failure, mismatch, database
errors, or a timeout (default 30 seconds; positive integer seconds only). It never
executes the task itself and refuses an inline backend. A timeout leaves the
probe queued or running for later inspection; it does not cancel it. Probe rows
remain until pruned.

`docker compose up --wait` confirms HTTP/database readiness and that the worker
container is running. `/health/` remains a web/database check; only the diagnostic
above verifies that a worker actually processes this queue. Run it after startup
or deployment, rather than repeatedly using it as a container health check.

For host-based development, stop the container web and worker, start PostgreSQL,
apply migrations, and run these in separate terminals:

```sh
uv run --locked --env-file .env python manage.py runserver
uv run --locked --env-file .env python manage.py db_worker --no-reload
uv run --locked --env-file .env python manage.py check_task_worker --timeout 30
```

## Failure inspection and recovery

Task state is `READY`, `RUNNING`, `SUCCESSFUL`, or `FAILED`. The backend's task
rows and UUIDs are operational records, not case authorization. Its upstream
global admin registration is disabled, including for superusers. The case Analysis workspace exposes its own case-scoped processing status;
operational task rows are never the product authorization boundary.

Use the operator shell for a metadata-only summary:

```sh
docker compose exec web python manage.py shell
```

```python
from django_tasks_db.models import DBTaskResult

list(
    DBTaskResult.objects.order_by("-enqueued_at").values(
        "id",
        "task_path",
        "status",
        "enqueued_at",
        "started_at",
        "finished_at",
    )[:20]
)
```

Worker logs record task identifiers, paths, and status through the existing private
logging handler. Exception tracebacks are excluded from routine task logs because
exception messages can contain sensitive input. The backend retains arguments,
results, exception details, and tracebacks in PostgreSQL. Treat that store and its
backups as private; inspect an individual traceback only in a trusted operator
session, and do not copy it into shared logs or tickets without sanitizing it.

There are no automatic task retries or stale-task reclamation. `READY` work waits
while workers are stopped and can run after restart. A crash or forced shutdown
can leave work `RUNNING`, even if some effects already completed; elapsed time
alone does not prove a worker is dead. Confirm the original worker has stopped,
review effects, and follow the future task's own idempotent recovery procedure
before enqueueing replacement work. Do not blindly reset running rows or create a
generic retry loop. For the harmless probe, correct the problem and rerun the
diagnostic. Failed and abandoned rows remain available for inspection.

Review retention manually. The supported housekeeping policy is 14 days for
successful results and 30 days for failures:

```sh
docker compose exec web python manage.py prune_db_task_results \
  --min-age-days 14 --failed-min-age-days 30 --dry-run
docker compose exec web python manage.py prune_db_task_results \
  --min-age-days 14 --failed-min-age-days 30
```

There is no automatic housekeeping schedule. Pruning removes finished task
results only; queued and running rows need deliberate review. Future evidence
provenance and processing history must live in case-scoped domain records,
independent of this disposable operational history.

## Adding a future task

Use module-level `@task` functions in an application's `tasks.py`, calling ordinary
domain functions. Pass JSON-compatible identifiers, such as UUID strings, and
reload records in the worker. Do not enqueue model objects, file objects, evidence
bodies, credentials, or invitation tokens. Return small operational results rather
than evidence contents. Changing task import paths or argument shapes requires
draining or deliberately migrating old queued work before deployment.

For this backend, enqueue the task in the same database transaction as its
originating domain write. Both records use Django's default PostgreSQL connection:
other connections cannot see either before commit, and rollback removes both.
For example, within a future authorized submission service:

```python
from django.db import transaction
from my_app.tasks import process_submission  # Example future task, not implemented.

with transaction.atomic():
    submission = create_authorized_submission()  # Example domain operation.
    process_submission.enqueue(str(submission.pk))
```

Do not defer that database queue insert to `on_commit`: doing so would introduce a
gap between committing the submission and saving its queued work. This atomicity
depends on the current same-database backend; reassess it if the backend changes.

Each future processing feature must enforce current case access and the required
role inside the worker, check same-case references, and account for revocation
before further processing or publication. Bound time, input sizes, external
requests, and concurrency; make effects idempotent and publish complete results
atomically. Preserve originals and processing provenance. Sanitize errors and
implement visible progress and a bounded recovery policy for that operation.
Image OCR runs locally with Tesseract as documented in [extraction](analysis-extraction.md).
Other inference defaults to pinned private runtimes through the shared backend
integration. External inference uses Cloudflare AI Gateway only when explicitly
selected and approved. Local failure never triggers cloud fallback. Inference
runtime limits are separate from these parser limits; see
[local analysis](local-analysis.md) for setup, identity checks and recovery.
The queue itself supplies none of these domain safeguards.

See [operations](operations.md) for coordinated backup and upgrades and
[validation](validation.md) for PostgreSQL and separate-process smoke coverage.
References: [Django Tasks](https://docs.djangoproject.com/en/6.1/topics/tasks/),
[django-tasks-db](https://github.com/RealOrangeOne/django-tasks-db).
