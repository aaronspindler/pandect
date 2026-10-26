# Pandect documentation

## Application foundation

- [Local development, configuration, and uv](development.md)
- [Architecture and implemented scope](architecture.md)
- [Case workflows, invitations, collaboration, and Django admin](cases.md)
- [People, contact details, and case-scoped administration](people.md)
- [Vehicles, identifying details, and case-scoped administration](vehicles.md)
- [Evidence uploads, private downloads, retention, and integrity](evidence.md)
- [Validation, CI, and Dependabot](validation.md)
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
are implemented alongside scoped operator administration. Evidence processing, AI,
and visual analysis remain planned capabilities.

## Interface design and delivery

- [Project design standard](../DESIGN.md)
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

References: [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [configuration layering and profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles).
