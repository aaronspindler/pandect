# Background-worker foundation

Django's Tasks API and `django-tasks-db` use Pandect's PostgreSQL database and one
`default` queue shared by web/workers, asynchronously even with `DEBUG=true`.
There is no Redis, scheduler or external queue. `core` owns diagnostics/AI
integration; its `core.analysis` subpackage owns case analysis.

The nonce diagnostic, [analysis](analysis.md) and
[vehicle CSV imports](vehicle-location-imports.md) share this queue. Domain records
retain case-scoped progress/recovery independently. Eligible evidence and its
checks commit together; original retention, invitations and file cleanup remain
synchronous. Other lengthy ingestion must implement authorization, idempotency
and recovery before using the queue.

**Rerun all analysis** retains a `CaseAnalysisBatch` cursor. Preparation queues at
most 25 active sources per task, traversing call additions within source limits,
and commits runs, cursor and continuation atomically. Each step rechecks verified
requester/edit access; run workers check again. Unavailable originals are counted
and skipped, retaining up to 100 safe source errors. Preparation failures stop
with a safe message; fresh requests preserve committed runs. Abrupt worker loss
may leave preparation pending. Confirm the old worker stopped before restarting;
there is no stale-task reclamation.

**Reprocess evidence** uses a `reprocess` batch scope retaining evidence types,
mode and reviewed execution snapshots. Bounded continuations prepare independent
and grouped methods plus extraction/transcription follow-ups. External-only file
analysis may need local prerequisites. AI retains separate local/Gateway identities;
geographic lookup follows configured service classification. Continuations recheck
verified edit access and approved settings; drift stops preparation, while queued
runs enforce their own drift checks. Method failures preserve other results.
Failed preparation attempts are counted separately from sources, with up to 100
safe source/method errors. Grouped comparisons disclose their retained coverage.

## Investigator progress and completed imports

**Processing** separates active work/history and preparation/execution.
**Checks queued** means preparation finished; runs/follow-ups may still be active.
New runs carry explicit parent-batch references, preserved through retries and
follow-ups even when another authorized editor retries. Historical membership is
never guessed. See the [Processing workflow](interface-design.md#case-tools-processing-and-additional-analysis)
for filters, review, recovery, and automatic updates to visible progress/results.
Refresh remains available without JavaScript; there is no notification scheduler.

Completed vehicle and HTML iMessage imports offer deliberate **Run local checks
on imported evidence**. Scope comes from retained relationships, published
message IDs/source rows on historical results or publishing runs on new CSV
contexts, never browser-supplied IDs. Each step queues at most 500 sources or the
configured lower limit; text groups also respect one million characters. Skipped
duplicates are excluded. These independent checks run no models, transcription,
external lookup or split context-dependent movement/conversation analysis.
Repeated submissions recover the same batch. Continuations recheck access;
preparation/run failures remain recoverable.

Confirmed message CSV publication instead atomically starts an automatic batch
for new messages with the import review's selected mode: Local checks, AI,
or Both. Local checks is the default and runs no models; AI runs one model suite;
Both runs local checks and that suite. AI selects Gateway when fully configured,
otherwise local/private inference, before submission. The resolved route is
reviewed and retained; external transfer requires explicit approval. AI is never
duplicated across backends or redirected after a runtime failure.
Bounded continuations walk
reported-time order, preparing the selected
[message/writing/model suite](analysis-communications.md#message-csv-imports).
Reviewed execution destinations remain bound to the batch and are rechecked
during preparation; changed settings stop preparation for fresh review. Reruns
require renewed external approval. Model failures preserve imports/independent
results without cloud fallback.
Preview, excluded and reused rows start no batch. No database migration or startup
backfill is needed. Imports stream without a row ceiling within file/message
limits, retaining compact summaries and paginated reviews/messages. All messages
and requested people still publish atomically; processing groups are not import
limits.

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
and recovery never block trip summaries. Maps use reported positions or reviewed
local estimates; lookup/review changes refresh geometry without resegmentation.
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
unexpected exits and allows 30 seconds before forced shutdown. Rebuild/restart
workers after source changes; they do not reload. Hosted deployments apply
migrations once before starting web/workers.

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

Private worker logs contain task IDs, paths and status, excluding exception
tracebacks because messages may contain sensitive input. PostgreSQL retains
arguments, results, exceptions and tracebacks. Protect it and its backups; inspect
tracebacks only in trusted sessions and sanitize before sharing.

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
