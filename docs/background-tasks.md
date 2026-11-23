# Background-worker foundation

Django's Tasks API and `django-tasks-db` use Pandect's PostgreSQL database
asynchronously even with `DEBUG=true`. Ordinary work uses the `default` queue.
There is no Redis, scheduler or external queue. `core` owns diagnostics/AI
integration; its `core.analysis` subpackage owns case analysis.

The nonce diagnostic, [analysis](analysis.md) and
[vehicle CSV imports](vehicle-location-imports.md) share this queue. Domain records
retain case-scoped progress/recovery independently. Eligible evidence and its
checks commit together; original retention, invitations and file cleanup remain
synchronous. Other lengthy ingestion must implement authorization, idempotency
and recovery before using the queue.

Automatic analysis and **Reprocess evidence** retain `CaseAnalysisBatch`
scopes with exact evidence or import membership, or selected evidence types for
reprocessing. The scope captures the case's selected, eligible AI routes.
Bounded continuations prepare independent and grouped methods,
commit the cursor and next task atomically, and schedule follow-ups from exact
extraction results. They recheck verified edit access, execution settings, and
case approval. Changes stop affected routes. Method failures preserve other
results. Preparation retains up to 100 safe source/method errors.

**Summarize missing instances** retains the reviewed saved group IDs and selected
execution route in an `instance_summaries` batch. Each preparation task visits up
to ten instances and queues one summary per eligible instance.
Unavailable instances are counted in Processing, where investigators can also
follow or retry individual runs. Preparation rechecks verified edit access and
the execution snapshot before each step.

## Investigator progress and completed imports

**Processing** separates active work/history and preparation/execution.
**Checks queued** means preparation finished; runs/follow-ups may still be active.
An individual check shows **Queued**, **Checking sources**, **Running analysis**,
or **Saving results**. Running checks show their start time. The progress bar is
indeterminate while the worker runs a method or saves its result. The outcome
appears once publication commits. Imports and batch preparation show measured
row, source, or check counts as they become available.
**Needs attention** links to stopped preparation, vehicle imports, and current
trip or address preparation failures. Batch details lead to a new request or the
existing import review when one is available. Earlier attempts remain inspectable
in history.

**Retry all failed checks** lets a case owner or editor request new attempts
across all pages of failed check history. Retry preparation runs in the background
and reports how many checks were reviewed, queued, or could not be retried.
Checks with a newer attempt are skipped. Each retry keeps its original selection
and uses the current service for its original processing backend. Preparation
rechecks edit access, source versions, and case approval. Resolve any blockers
and use the same action to retry checks still in need of attention.

New runs carry explicit parent-batch references, preserved through retries and
follow-ups even when another authorized editor retries. Historical membership is
never guessed. See the [Processing workflow](interface-design.md#case-tools-processing-and-additional-analysis)
for filters, review, recovery, and automatic updates to visible progress/results.
Refresh remains available without JavaScript; there is no notification scheduler.

When [private meaning search](local-analysis.md#private-case-search-embeddings)
is configured, Processing shows the case index and its progress. The worker
queues missing indexes after migrations and rechecks case permissions before
embedding source text. After correcting a failure, an owner or editor can retry
from Processing. Keyword search is available while passages are being indexed.

Confirmed vehicle and message CSV publication starts analysis for newly published
observations and messages using retained import relationships. Preview, excluded
and reused rows start no batch. Vehicle trip preparation is independent. Bounded
continuations retain each run's scope and distinguish preparation from execution.
Processing groups do not limit the number of rows an import can publish within
the existing file and row constraints.

On upgrade, apply the case approval migration before serving with matching web
and worker versions. Finish earlier authorized external work before cutover if
needed. Any remaining external run without a case approval stops recoverably.
Existing results remain available and cases receive no retrospective processing.
Before starting older workers during rollback, stop queued grant-backed external
work or disable external services. Older workers cannot enforce case approval.

## Instagram monitor queue

The optional [offline pilot](instagram-monitors.md) runs on the `instagram`
queue in the same PostgreSQL database. Compose starts one dedicated
`instagram-worker` with the `instagram-pilot` profile. Ordinary workers
consume `default` so roster collection leaves capacity for evidence jobs.
Each check shares one collection slot per personal connection and rechecks
case access and target approval before capture and publication. A completed
check schedules its successor in the same transaction. Processing shows
case-scoped recovery. Live scheduling requires a qualified roster source.

## Saved vehicle trips

`geo` owns vehicle trip state, generations, retained inputs and ordered membership.
Authorized evidence/import/archive changes invalidate the revision and atomically
queue rebuilding. Each task handles 500 observations at most, committing staged
output, cursor/token and continuation together. Active/archived/all variants share
a generation. Processing rechecks verified edit access, source relationships and
revision; only complete current generations publish. Failed/superseded work stays
unpublished. Total history has no 100/500-observation cutoff.

Map/report/vehicle pages read saved trips. **Prepare trips** initializes existing
evidence through an authorized idempotent POST; GET/startup remain read-only.
Processing exposes progress/history. Retries preserve prior generations, use new
delivery tokens and allow three attempts. Running-work recovery requires explicit
confirmation that its worker stopped. Source deletion inventories include every
dependent generation, including observations producing no trip; deletion
transactions invalidate/rebuild remaining evidence. A superuser with viewer-only
access can confirm complete-inventory deletion but cannot queue rebuilding; an
owner/editor prepares the replacement.

After publication, separate continuations prepare local geocode runs with at most
25 distinct addresses and the configured source ceiling per run. Their progress
and recovery never block trip summaries. Maps use reported positions or selected
local estimates. Lookup or selection changes refresh geometry without resegmentation.
See [trip rules](analysis-locations.md#automatic-vehicle-trips) and
[map lookups](maps.md#local-address-lookup).

## Startup and diagnostics

After [local setup](development.md#first-run), verify execution and inspect logs:

```sh
docker compose exec web python manage.py check_task_worker --timeout 30
docker compose logs --tail 100 worker
```

Workers share web's image, configuration and private volume. After migrations/web
readiness they run `python manage.py db_worker --no-reload`. Compose restarts
unexpected exits. Use [`make run`](development.md#first-run) to rebuild after source
changes and let each worker finish its active task before replacement. A regular
`docker compose stop` gives the default worker 30 seconds before forced shutdown.
Hosted deployments apply migrations once before starting web/workers.

`check_task_worker` enqueues `core.tasks.worker_probe` with a fresh nonce, waits
for its matching result and prints the task UUID and `OK`. Failure, mismatch,
database error or timeout exits nonzero. Timeout defaults to 30 positive integer
seconds. The command refuses inline backends and never executes the task itself;
a timeout leaves it queued/running, and probe rows remain until pruned.

`docker compose up --wait` verifies HTTP/database readiness and a running worker
container; `/health/` checks web/database only. The diagnostic verifies queue
execution. Run it after startup/deployment, not as a recurring container health check.

For [host development](development.md#run-python-on-the-host), start web/workers,
then run:

```sh
uv run --locked --env-file .env python manage.py check_task_worker --timeout 30
```

### Parallel local workers

Each `db_worker` process executes one task at a time. For independent external AI
jobs, add workers to the existing local Compose project:

```sh
docker compose up -d --no-deps --no-build --pull never --no-recreate \
  --scale worker=4 worker
docker compose ps worker
```

This starts a bounded pool of four workers using the existing application image,
without replacing the worker already processing a task. Verify the image tag still
matches the running web/worker image before scaling. PostgreSQL locks give each
worker a different queue task; analysis claims and publication retain their own
authorization, run locks and atomic transactions. No queue or execution-route
changes are needed, and previously approved external jobs retain their destinations.

Ordinary workers consume the same queue, including local checks, indexing and imports.
Review pending operations and private inference capacity first: the private
transcription service accepts only one request at a time, and overlapping requests
fail recoverably as `local_ai_busy`. More workers also increase simultaneous
external AI requests and can encounter provider rate limits. Observe completed
domain runs and request failures after scaling. Keep the requested replica
count explicit in later scaling commands.

Message-import preparation queues each bounded page's runs before its continuation.
Parallel workers shorten that wait, but the preparation count can still pause while
those runs execute.
Before reducing replicas or restarting workers, let active operations finish;
forced shutdown can leave abandoned running work requiring the recovery below.

## Failure inspection and recovery

Task states are `READY`, `RUNNING`, `SUCCESSFUL` and `FAILED`. Operational rows/UUIDs
grant no case access. `core.admin` disables upstream global queue administration,
including for superusers; Processing exposes case-scoped domain history.

Inspect metadata in a trusted operator shell:

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

Private worker logs contain task IDs, paths and status. Failed analysis also logs
its run UUID, known operation, safe error code, execution stage, exception class,
and at most eight application code locations (module, function and line number).
Model validation adds a fixed category for input size, result structure,
suggestion citations, citation structure, citation source, required quotation
offset, or quotation mismatch. Logs exclude exception messages, source lines,
local variables, evidence text and model responses. PostgreSQL retains task
arguments, results and uncaught task exceptions/tracebacks. Protect the database
and its backups. Inspect tracebacks only in trusted sessions and sanitize before
sharing.

Analysis tasks can return normally after recording a failed domain run. Use
the trusted operator shell to compare the saved outcome with recent deliveries:

```sh
docker compose exec web python manage.py check_task_worker --analysis-run RUN_UUID
```

This read-only command shows the saved status and safe error code plus up to ten
retained queue deliveries. A `SUCCESSFUL` delivery can accompany a failed
analysis. `GatewayRequest` records external AI request status,
byte counts and token usage without response bodies. Known model validation
failures retain their safe error code. When a completed request is followed by
a generic `processing_failed` run, use the safe analysis log to locate the later
validation or publication failure.
Older failures without these diagnostics may have no recoverable
cause. Keep quotation validation strict. External retries require current case-owner approval for the destination.

No automatic retries or stale-task reclamation exist. `READY` tasks wait for
workers; crashes can leave `RUNNING` tasks with some effects completed. Elapsed
time alone does not establish that a worker stopped. Confirm the original worker stopped, review effects
and follow operation-specific idempotent recovery before replacement. Never
blindly reset rows or add generic retry loops. Correct problems and rerun harmless
probes. Failed/abandoned rows remain inspectable.

Manually review retention: successful results keep 14 days, failures 30 days.

```sh
docker compose exec web python manage.py prune_db_task_results \
  --min-age-days 14 --failed-min-age-days 30 --dry-run
docker compose exec web python manage.py prune_db_task_results \
  --min-age-days 14 --failed-min-age-days 30
```

Housekeeping is unscheduled and removes only finished results. Review queued/running
rows deliberately. Case-scoped evidence provenance/history outlive operational rows.

## Adding a future task

Define module-level `@task` functions in the owning app's `tasks.py`, calling domain
functions. Pass JSON-compatible IDs and reload records; enqueue no models, files,
evidence bodies, credentials or invitation tokens. Return small operational
results. Drain or deliberately migrate queued work before changing task paths or
argument shapes.

Enqueue inside the originating domain transaction. Both records use the default
PostgreSQL connection, so neither is visible before commit and rollback removes
both. For example, in a future authorized service:

```python
from django.db import transaction
from my_app.tasks import process_submission  # Example future task, not implemented.

with transaction.atomic():
    submission = create_authorized_submission()  # Example domain operation.
    process_submission.enqueue(str(submission.pk))
```

Using `on_commit` would leave a domain-write/queue-insert gap. Reassess this
atomicity if the backend changes.

Workers must recheck current case/role access, same-case references and revocation
before further processing/publication. Bound time, inputs, requests and concurrency;
make effects idempotent and publication atomic. Preserve originals/provenance,
sanitize errors and provide progress/bounded recovery. The queue supplies none of
these safeguards. See [local inference](local-analysis.md) for routing/runtime
limits, [extraction](analysis-extraction.md) for OCR,
[operations](operations.md) for backup/upgrades and [validation](validation.md)
for PostgreSQL/separate-process checks.

References: [Django Tasks](https://docs.djangoproject.com/en/6.1/topics/tasks/),
[django-tasks-db](https://github.com/RealOrangeOne/django-tasks-db).
