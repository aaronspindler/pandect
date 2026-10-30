# Pandect documentation

## Application foundation

- [Local development, configuration, and uv](development.md)
- [Architecture and implemented scope](architecture.md)
- [Background-worker setup, diagnostics, and recovery](background-tasks.md)
- [Case analysis, reports, processing, and AI configuration](analysis.md)
- [Local inference setup, model qualification, and private map services](local-analysis.md)
- [Automatic geographic context and retained map references](analysis-geographic-context.md)
- [Case workflows, invitations, collaboration, and Django admin](cases.md)
- [Investigation map, OpenStreetMap tiles, and location coverage](maps.md)
- [People, contact details, and case-scoped administration](people.md)
- [Vehicles, identifying details, and case-scoped administration](vehicles.md)
- [Evidence, communications, retained call material, and private downloads](evidence.md)
- [Vehicle locations and Asset Location CSV imports](vehicle-location-imports.md)
- [Validation, CI, and Dependabot](validation.md)
- [October 2026 code review catalogue and validation](code-review-2026-10.md)
- [Compose operation, backup, and recovery](operations.md)

The Django project was generated from the repository root using
`python -m django startproject config .`. Its configuration lives in `config/`:

| Configuration | Module path |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` in `manage.py`, WSGI, and ASGI | `config.settings` |
| `ROOT_URLCONF` | `config.urls` |
| `WSGI_APPLICATION` | `config.wsgi.application` |
| `ASGI_APPLICATION` | `config.asgi.application` |
| WSGI server entry point | `config.wsgi:application` |
| ASGI server entry point | `config.asgi:application` |

The initial migrations use `accounts.User`; application and integration tests use
PostgreSQL. Account screens, case management, email invitations, viewer/editor
collaboration, people and vehicle management, and retained evidence uploads/downloads
are implemented alongside scoped operator administration and bounded vehicle-location
CSV imports. Bounded extraction and investigation workflows are documented in
[case analysis](analysis.md); the broader catalogue remains a planning reference.

## Product planning

- [Evidence analysis and extraction catalogue (proposed capabilities)](evidence-analysis.md)
- [Evidence analysis implementation roadmap (proposed priorities)](evidence-analysis-roadmap.md)

## Interface design and delivery

- [Project design standard](../DESIGN.md)
- [Public marketing pages and hosted-service messaging](marketing.md)
- [Investigator screens, workflows, and density specification](interface-design.md)
- [Interface implementation plan and acceptance criteria](interface-plan.md)
- [Browser validation and local captures](validation.md#browser-checks)

The implemented interface follows the compact three-pane investigation workspace.
The design documents remain the reference for behavior and acceptance criteria;
validation results must be reported separately from implementation status.

## Codex project configuration

The project defaults live in [`.codex/config.toml`](../.codex/config.toml).
Open a new Codex session in this repository to load them. Codex must trust the
project before it loads project configuration; explicit session overrides and
managed requirements can take precedence.

| Setting | Project default |
| --- | --- |
| Implementation model | GPT-6.1 Sol (`gpt-6.1-sol`) |
| Implementation reasoning | `xhigh` |
| Response verbosity | Low (concise answers) |
| Plan mode reasoning | `ultra` |
| Speed | Fast (`service_tier = "fast"`) |
| Change approvals | Automatic review (`auto_review`) |
| Sandbox | `workspace-write` |
| Internet documentation | Live built-in web search |

The intended planning model is GPT-6 Astra (`gpt-6-astra`). Codex CLI 0.164.0
supports a separate Plan mode reasoning setting, but has no project setting
that automatically selects a different model for Plan mode. Select Astra when
entering Plan mode, and select Sol 6.1 with `xhigh` effort before implementation.
Keep Fast enabled for both.

In the CLI, start with `codex`, use `/model` to choose Astra and Ultra, then enter
`/plan`. Before implementing, leave Plan mode and use `/model` to choose Sol 6.1
and Extra high. Fast is enabled by the project config; `/fast` controls it in
the CLI. Merely leaving Plan mode does not configure a model switch back to Sol.

Automatic review routes approval requests to Codex's reviewer. Actions permitted
by the sandbox can proceed directly; automatic review may deny other requests.
Live documentation lookup uses built-in web search without enabling unrestricted
network access for shell commands.

Configuration was checked against the installed Codex CLI 0.164.0 and its bundled
model catalog. These settings do not change an already-running conversation.

### Simplicity review

The project agent
[`simplicity_reviewer`](../.codex/agents/simplicity_reviewer.toml) checks the final
task changes for overengineering and unnecessary bloat before handoff. It
inherits the parent model and reasoning settings and is instructed to inspect
without making edits. Its permissions still come from the parent session.

As required by [the contributor guide](../AGENTS.md#simplicity-and-maintainability),
the implementing agent supplies the requirements and final scoped diff (including
new files), waits for the review, and resolves material findings before handing
back changes. The reviewer identifies concrete maintenance costs and simpler
alternatives while preserving the project's correctness and integrity requirements.
Substantive fixes require affected checks and another review.

Open a new trusted Codex session to discover the custom agent. You can request it
explicitly: "Have simplicity_reviewer review the final changes for this task."
The handoff requirement is an agent instruction, not a Git or CI hook. If the
client cannot load custom agents, delegate the review using the instructions in
the same TOML file and disclose that fallback.

References: [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
[configuration layering and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles),
and [custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents).
