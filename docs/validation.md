# Validation and automation

## Local checks

Start the development database and load the same environment used by Django:

```sh
uv sync --locked
docker compose up -d --wait db
uv run --locked --env-file .env python manage.py migrate
uv run --locked --env-file .env make check
make compose-smoke
make security-check
```

`make check` runs Ruff lint/format checks, Django tests against PostgreSQL, uv
lockfile validation, Django database/system checks, migration drift detection,
and Compose configuration validation. The database role must be able to create
and delete Django's `test_<POSTGRES_DB>` database. Never point tests at an
investigation or shared hosted database.

`make lint`, `make test`, and `make validate` run individual groups. Management
checks need environment variables; use the `uv run --env-file .env` wrapper when
calling them locally. `make format` applies Ruff fixes and formatting.

Tests cover the custom user, password hashing, sign-in failure, email verification,
registration, CSRF, sign-out semantics, staff access, and database readiness and
failure. Case tests cover required ownership, stable creation timestamps,
collaborator roles and constraints, protected owners, and membership cleanup.
Admin tests cover model-permission gates, scoped lists and direct URLs (including
history and deletion), owner-only changes, unrelated superusers, revoked access,
CSRF, and forged ownership, membership IDs, parent cases, and roles.

`make compose-smoke` needs Docker only. It creates a unique project with temporary
synthetic configuration, random loopback ports, and fresh volumes. It builds the
runtime image, checks initial migrations and HTTP readiness, creates a synthetic
owner, collaborator, case, and membership, stops and restarts the stack, and
verifies their persistence. Its exit trap removes only that project's containers
and volumes. It never uses your `.env` or data.

`make security-check` exports the hash-locked dependency graph, including developer
tools, and runs pip-audit against it without re-resolving dependencies. It requires
internet access to the advisory service. Temporary exports are removed on exit.

## GitHub Actions

- **CI** runs the same checks, fresh PostgreSQL migrations, and Compose smoke on
  Linux amd64 and arm64. uv reads `.python-version` and installs from `uv.lock`.
- A separate CI job audits locked dependencies without database credentials.
- **Scheduled security audit** reruns the dependency audit weekly and on demand.
- **CodeQL** analyzes Python on pushes, pull requests, and a weekly schedule. The
  existing public-repository guard remains: private repositories skip this job
  unless the project deliberately configures an eligible code-scanning plan.

Workflows use read-only contents permissions and pinned action commits. The
rebuild's previous secret/image audit scripts no longer exist; these workflows
make no claim to perform those scans. Container and secret scanning can be added
as separately maintained checks.

Local checks do not prove hosted workflow execution or deployed behavior. Hosted
checks run only after changes reach GitHub.

## Dependabot

The weekly configuration uses the native `uv`, `docker`, and `github-actions`
ecosystems. Each ecosystem groups version updates into one PR, retaining a limit
of five open PRs per ecosystem. Native uv updates maintain both `pyproject.toml`
and `uv.lock`, including the development group. Docker discovers the Dockerfile
and Compose images. PostgreSQL major updates are excluded because they require
a planned database upgrade. Security updates follow GitHub's separate settings;
these groups do not opt into grouping security updates.

Review runtime updates against the coordinated pins in
[architecture](architecture.md); a Docker update alone does not update
`.python-version` or `mise.toml`. New uv and Django major versions may require
adjusting the declared compatibility ranges.

References: [uv and GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/),
[uv and Dependabot](https://docs.astral.sh/uv/guides/integration/dependabot/).
