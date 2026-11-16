# Background-worker foundation

Pandect uses Django's native Tasks API with `django-tasks-db` and its existing
PostgreSQL database. One `default` queue is shared by web and worker processes.
There is no Redis service, scheduler, or external queue. Shared queue diagnostics
and AI integration belong to `core`; case analysis lives in its `core.analysis`
subpackage. The backend remains asynchronous when `DEBUG=true`.

The synthetic diagnostic returns a nonce; [case analysis](analysis.md) and
[vehicle-location CSV imports](vehicle-location-imports.md) also use this queue
with separate durable case-scoped records, progress, and recovery.
Eligible investigator evidence and its automatic checks are committed together
through the existing PostgreSQL queue. Original retention, invitations, and file
cleanup are synchronous. Other lengthy ingestion must add its own
authorization, idempotency, and recovery before using this foundation.

Case-wide **Rerun all analysis** retains a case-scoped `CaseAnalysisBatch` cursor.
Each preparation task queues local runs for at most 25 active sources and commits
the cursor, those runs and the next task atomically. Calls are traversed by retained
addition, so large call histories do not exceed per-run source limits. Each step
rechecks the requester's current editing access and verified account; individual
analysis workers recheck access again. Unavailable originals are counted and skipped; up to 100 source-specific safe
preparation errors are retained. Preparation failures stop the batch with a safe message; investigators
can make a fresh request from Processing. Previously committed runs remain retained.
As with other tasks, abrupt worker loss can leave preparation pending. Confirm the
old worker has stopped before making a fresh request; the queue does not reclaim
stale work automatically.

## Investigator progress and completed imports

The main **Processing** screen separates active work from retained history, and
batch preparation from run execution. A batch marked **Checks queued** has
finished preparing work; its runs and extraction follow-ups may still be queued
or running. New runs carry an explicit parent-batch
reference. Retries and their automatic follow-ups preserve that reference, including
when another authorized editor requests the retry. Historical runs without that
reference are not assigned a guessed batch.

See the [Processing workflow](interface-design.md#case-tools-processing-and-additional-analysis)
for history filters, import review, and recovery controls. Refresh provides in-app
completion feedback; there is no notification scheduler.

Completed vehicle and HTML iMessage imports offer **Run local checks on imported
evidence** deliberately. Scope comes from retained import relationships (and the
published iMessage IDs and source rows for historical results, or the publishing
run on new CSV message contexts), never browser-supplied evidence IDs. Each
local-check step queues independent local checks for at most 500 sources or the configured lower
source limit; text groups also respect the one-million-character analysis bound.
Skipped duplicate rows are absent. This does not run models, transcription,
external lookups, or split context-dependent movement/conversation analyses.
Duplicate submissions recover the same batch. Each continuation rechecks access;
preparation and run failures remain recoverable through existing workflows.

Confirmed message CSV imports instead start their message-analysis batch
automatically in the publication transaction. The existing batch cursor and task
continuations prepare bounded selections in reported-time order. They queue both
independent local checks and the message/writing/model suite described in
[automatic message analysis](analysis-communications.md#message-csv-imports).
Processing separates preparation from execution; **Checks queued** does not mean
model work has finished. Model failures preserve the import and independent
results, and never trigger cloud fallback. Preview, excluded, and reused rows do
not start a batch. No database migration or startup backfill is required.

Message CSV imports have no row-count ceiling. They stream within the configured
file and per-message limits, retain compact analysis summaries, and paginate
source review and completed messages. Publication still commits all messages and
requested new people atomically; internal processing groups are not import limits.

## Saved vehicle trips

`geo` owns durable per-vehicle trip state, generations, every retained input and
ordered trip membership. Authorized evidence/import/archive mutations invalidate
the vehicle revision and atomically queue replacement work. Each task handles at
most 500 observations and commits staged output, its cursor/token and continuation
together. Active/archived/all variants share one generation. Current verified
editor access, source relationships and revision are checked during processing;
only a complete current generation is published. Failed or superseded work is
never exposed as complete. Input history has no 100/500-observation cutoff.

The Map, report and vehicle pages read saved trips. Existing evidence is initialized
through **Prepare trips**, a case-authorized idempotent POST; GET and application
startup remain read-only. Processing exposes progress and generation history.
Retries preserve prior generations, use new delivery tokens and allow three
attempts. Recovering running work requires explicit confirmation that its previous
worker stopped. Confirmed source deletion inventories include all dependent
generations, including those affected by observations that produced no trip;
remaining evidence is invalidated/rebuilt in the deletion transaction. A
superuser with viewer-only case access can confirm deletion of its complete
inventory but cannot queue a rebuild;
a current owner/editor prepares the replacement.

After publication a separate bounded continuation prepares existing local geocode
runs, at most 25 distinct addresses and the configured source ceiling per run.
Lookup preparation has its own progress/recovery and never blocks trip summaries.
The trip map uses only reported positions or specifically reviewed local estimates;
lookup/review changes refresh geometry without resegmenting originals. See
[trip rules](analysis-locations.md#automatic-vehicle-trips) and
[map lookups](maps.md#local-address-lookup).

## Startup and diagnostics

Start the stack using [local development](development.md#first-run), then verify
queue execution and inspect worker logs:

```sh
docker compose exec web python manage.py check_task_worker --timeout 30
docker compose logs --tail 100 worker
```

The worker uses the same image, configuration, and private storage volume as web.
It starts after web becomes healthy, following migrations, and runs
`python manage.py db_worker --no-reload`. Compose restarts unexpected worker exits
and allows 30 seconds for graceful shutdown before forcing termination. Restart
or rebuild the worker after source changes; it does not reload automatically.
For hosted deployments, apply migrations once before starting web and workers.

Core's `check_task_worker` command enqueues `core.tasks.worker_probe` with a fresh
random nonce, waits for its matching result, and prints the task UUID and `OK`.
It exits nonzero for failure, mismatch, database
errors, or a timeout (default 30 seconds; positive integer seconds only). It never
executes the task itself and refuses an inline backend. A timeout leaves the
probe queued or running for later inspection; it does not cancel it. Probe rows
remain until pruned.

`docker compose up --wait` confirms HTTP/database readiness and that the worker
container is running. `/health/` remains a web/database check; only the diagnostic
above verifies that a worker actually processes this queue. Run it after startup
or deployment, rather than repeatedly using it as a container health check.

For [host-based development](development.md#run-python-on-the-host), run the
diagnostic after starting the documented web and worker processes:

```sh
uv run --locked --env-file .env python manage.py check_task_worker --timeout 30
```

## Failure inspection and recovery

Task state is `READY`, `RUNNING`, `SUCCESSFUL`, or `FAILED`. The backend's task
rows and UUIDs are operational records, not case authorization. Its upstream
global admin registration is disabled by `core.admin`, including for superusers.
The case Processing screen exposes case-scoped processing history;
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
review effects, and follow the operation's documented idempotent recovery procedure
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
results only; queued and running rows need deliberate review. Evidence provenance
and processing history live in case-scoped domain records, independent of
disposable operational history.

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
Follow [local analysis](local-analysis.md) for inference routing and separate runtime
limits; [extraction](analysis-extraction.md) owns local OCR support and limits.
The queue itself supplies none of these domain safeguards.

See [operations](operations.md) for coordinated backup and upgrades and
[validation](validation.md) for PostgreSQL and separate-process smoke coverage.
References: [Django Tasks](https://docs.djangoproject.com/en/6.1/topics/tasks/),
[django-tasks-db](https://github.com/RealOrangeOne/django-tasks-db).
