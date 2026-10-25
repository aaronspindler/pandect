# Pandect documentation

## Application foundation (planned)

The rebuild must use the latest stable [Python](https://www.python.org/downloads/)
and [Django](https://www.djangoproject.com/download/) releases. Verify their
[compatibility](https://docs.djangoproject.com/en/stable/faq/install/#what-python-version-can-i-use-with-django)
when implementing the application, and keep dependency metadata, lockfiles,
runtime pins, CI, and container images aligned as versions change.

Create the Django project from the repository root with
`python -m django startproject config .`. The configuration package must be
`config/`, containing settings, URL configuration, `wsgi.py`, and `asgi.py`.
Use these module paths throughout application and deployment configuration:

| Configuration | Module path |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` in `manage.py`, WSGI, and ASGI | `config.settings` |
| `ROOT_URLCONF` | `config.urls` |
| `WSGI_APPLICATION` | `config.wsgi.application` |
| `ASGI_APPLICATION` | `config.asgi.application` |
| WSGI server entry point | `config.wsgi:application` |
| ASGI server entry point | `config.asgi:application` |

Application scaffolding is not yet present. Configure the custom user model and
PostgreSQL before running initial migrations, as required by [AGENTS.md](../AGENTS.md).

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
