# Background-worker foundation

Django's Tasks API and `django-tasks-db` use PostgreSQL for background work,
including with `DEBUG=true`. The `default` queue handles most jobs. Pandect's
domain records own progress, authorization and recovery.

Automatic analysis and **Reprocess evidence** retain a `CaseAnalysisBatch` with
exact source/import membership or reviewed evidence types and eligible AI routes.
Each continuation commits its cursor and next task together, rechecking edit
access, execution settings and approval. Failed methods leave other results
intact. Preparation records up to 100 source or method errors.

Conversation grouping publishes a revision only after its source count and
checksums match the reviewed originals. A changed pair needs fresh grouping,
while other pairs and earlier publications stay available. **Summarize missing
instances** retains the reviewed groups and route. Preparation queues at most
ten instances at a time and uses bounded message ranges for long sessions.
See [communication analysis](analysis-communications.md).

## Investigator progress and completed imports

**Processing** shows active work and history. Its totals count work items,
including earlier attempts. A preparation batch and each of its checks count
separately. Trip preparation and address lookup count as one generation.
**Processed** includes checks that finished with partial results, no findings,
insufficient evidence, or unsupported input. Pending review, cancelled and
superseded work are outside the totals.

**In progress** shows queued and running work. **Needs attention** shows
pending CSV reviews, current failures, and stopped work. Imports and batch
preparation report counts as they become available. **Checks queued** means
preparation finished, while individual checks and follow-ups may still be
running. See the [Processing workflow](interface-design.md#case-tools-processing-and-additional-analysis)
for the investigator's view.

Owners and editors can **Retry all failed checks** across the saved history.
Retry preparation reports reviewed, queued and unavailable checks, and skips
checks with a newer attempt. It revalidates access, sources and approval, and
uses current settings for the original processing backend. Retries and
follow-ups retain the parent batch even when another editor retries.

Vehicle and message CSV publication queues analysis for newly published rows.
Preview, excluded and reused rows start no analysis batch. Vehicle trip
preparation runs independently. Call history publication rechecks edit access,
source integrity, people and overlap candidates before committing calls. Changed
overlaps return the import to review. Recover running imports only after the
original worker has stopped. See [vehicle CSV imports](vehicle-location-imports.md)
and [call history CSV imports](evidence.md#call-history-csv-import).

## Evidence chat recovery

A queue delivery advances a turn in the Cloudflare investigation Worker. Django
rechecks case access, the owner's approval revision, cutoff and remaining
budget for each operation and saves the reply in Pandect. Retry from the
affected reply. See [Case Chat recovery](cloudflare-agent.md#investigation-and-recovery)
for interrupted deliveries.

## Hosted search indexing

Enabling external AI queues work after the owner decision commits. Pandect
provisions the private case instance, syncs bounded passages and records coverage.
Archiving and deletion invalidate local retrieval immediately. Disabling AI fences
the active revision and queues remote cleanup. Recovery uses the durable instance
record even after case deletion. See [hosted search](cloudflare-search.md).

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

Source changes invalidate saved trip generations and queue successors for
authorized editors. Each task stages up to 500 observations with its cursor and
continuation. It rechecks access, retained inputs and revision. Only complete,
current generations publish. Use **Prepare trips** to initialize trips for
existing evidence. Running-work recovery requires confirmation the old worker
stopped. Complete-inventory deletion includes dependent generations. A
superuser with viewer-only access can delete after review, and a case editor
then prepares the replacement.

Address lookups use separate continuations of up to 25 distinct addresses.
Trips remain available while lookup runs, and estimate changes refresh the map
without rebuilding trips. See [trip rules](analysis-locations.md#automatic-vehicle-trips)
and [map lookups](maps.md#local-address-lookup).

## Startup and diagnostics

After [local setup](development.md#first-run), verify execution and inspect logs:

```sh
docker compose exec web python manage.py check_task_worker --timeout 30
docker compose logs --tail 100 worker
```

Workers share web's image, configuration and private volume. In development
Compose, workers start after the migrated web service is healthy. Rebuild using
[`make run`](development.md#first-run) with the active profiles so every worker
can finish its current task. A regular `docker compose stop` limits shutdown to
30 seconds for the default worker and 60 seconds for the Instagram worker.
In Dokploy, the singleton worker entrypoint applies pending migrations before it
consumes queue work. Verify its preparation marker before activating the new web
release. See [Dokploy deployment](operations.md#first-dokploy-deployment).

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

Workers share one queue for deterministic checks, search indexing, imports and
Cloudflare processing. Scaling increases concurrent requests against the account’s
search and model limits. Check those limits before scaling and monitor completed
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

Compare a saved analysis outcome with its ten most recent deliveries:

```sh
docker compose exec web python manage.py check_task_worker --analysis-run RUN_UUID
```

This read-only diagnostic shows the outcome and safe error code. A successful
delivery can accompany a failed analysis. `GatewayRequest` records status,
byte counts and token usage. For a generic `processing_failed` after a
completed request, inspect the safe analysis log for validation or publication
errors. External retries require current case-owner approval.

The queue framework leaves retries and stale-task recovery to each operation.
See [hosted search indexing](#hosted-search-indexing) for its retry schedule.
`READY` tasks wait for workers. Crashes can leave `RUNNING` tasks with some effects completed. Elapsed
time alone does not establish that a worker stopped. Confirm the original worker stopped, review effects
and follow operation-specific idempotent recovery before replacement. Never
blindly reset rows or add generic retry loops. Correct problems and rerun harmless
probes. Failed/abandoned rows remain inspectable.

Review pruning with a dry run. These commands remove successful deliveries at
least 14 days old and failed deliveries at least 30 days old. Without
`--queue-name`, the command covers only the `default` queue. The example includes
the Instagram queue so it also covers finished deliveries from a previously
enabled pilot.

```sh
docker compose exec web python manage.py prune_db_task_results \
  --queue-name default,instagram --min-age-days 14 --failed-min-age-days 30 --dry-run
docker compose exec web python manage.py prune_db_task_results \
  --queue-name default,instagram --min-age-days 14 --failed-min-age-days 30
```

Housekeeping is unscheduled and removes only finished results. Review queued/running
rows deliberately. Case-scoped evidence provenance/history outlive operational rows.

## Adding a future task

Define module-level `@task` functions in the owning app's `tasks.py`, calling domain
functions. Pass JSON-compatible IDs and reload records; enqueue no models, files,
evidence bodies, credentials or invitation tokens. Return small operational
results. Drain or deliberately migrate queued work before changing task paths or
argument shapes.

Enqueue within the domain transaction when the write and task must commit
together. Both use the default PostgreSQL connection. An `on_commit` callback
would leave a gap for a failed queue insert.

Workers recheck case/role access, same-case references and revocation.
Bound resources, preserve originals and provenance, sanitize errors, make
effects idempotent, and publish atomically. Domain workflows own progress and
recovery. See [validation](validation.md) for separate-process checks.

References: [Django Tasks](https://docs.djangoproject.com/en/6.1/topics/tasks/),
[django-tasks-db](https://github.com/RealOrangeOne/django-tasks-db).
