# Pandect documentation

[PRINCIPLES.md](../PRINCIPLES.md) owns foundational project principles;
[AGENTS.md](../AGENTS.md) owns contributor requirements;
[DESIGN.md](../DESIGN.md) owns durable interface standards. The guides below own
current behavior and procedures. Start with the guide for the task; follow its
links for domain details. Historical reports are kept in the [archive](archive/README.md)
and are not current implementation or validation guidance.

## Setup and operation

- [Development, configuration, and uv](development.md)
- [Architecture, runtime versions, app ownership, and migration baseline](architecture.md)
- [Background workers, diagnostics, and recovery](background-tasks.md)
- [Operations, storage, backup, and recovery](operations.md)
- [Dokploy deployment](operations.md#first-dokploy-deployment)
- [Validation, browser checks, CI, and Dependabot](validation.md)

## Investigation workflows

- [Cases, invitations, permissions, and scoped administration](cases.md)
- [People](people.md) and [vehicles](vehicles.md)
- [Evidence, communications, retention, and private downloads](evidence.md)
- [Vehicle locations and Asset Location CSV imports](vehicle-location-imports.md)
- [Analysis, reports, review, and Gateway configuration](analysis.md)
- [Original document and recording previews](original-previews.md)
- [Chronology](chronology.md)
- [Shared selections and printable briefs](shared-selections.md)
- [Question-driven synthesis](synthesis.md)
- [Local inference, model qualification, and private geographic services](local-analysis.md)
- [Geographic context and retained map references](analysis-geographic-context.md)
- [Investigation maps and location coverage](maps.md)

## Design and planning

- [Investigator screens and workflows](interface-design.md)
- [Design references and assets](design/README.md)
- [Public marketing pages](marketing.md)
- [Project descriptions and reusable marketing copy](marketing-copy.md)
- [Analysis roadmap](evidence-analysis-roadmap.md)
- [Archived planning, reviews, and qualification reports](archive/README.md)

## Codex project configuration

[`.codex/config.toml`](../.codex/config.toml) is the source of project defaults.
Start a new trusted Codex session to load it; session overrides and managed
requirements may take precedence. See the official
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [configuration layering](https://learn.chatgpt.com/docs/config-file/config-basic).

The planning preference is GPT-6 Astra with Ultra reasoning; implementation uses
GPT-6.1 Sol with Extra high reasoning. Select the model explicitly when changing
modes: `plan_mode_reasoning_effort` sets effort only. In the CLI, use `/model`,
then `/plan`; after leaving Plan mode, select the implementation model again.
Keep Fast enabled for both. Model/effort availability depends on the client and
account; project defaults do not change an already-running conversation.

Automatic review handles eligible approval requests, not all actions. Sandbox
permissions and shell network restrictions still apply; built-in live web search
does not grant unrestricted shell networking.

### Simplicity review

Use the shared Agentics
[`simplicity_reviewer`](https://github.com/aaronspindler/agentics) for the
[required handoff review](../AGENTS.md#simplicity-and-maintainability). It inherits
the parent model/reasoning and stays read-only within the parent permissions.
Supply task requirements, the final scoped diff including new files, and the
relevant Pandect safeguards. Exclude unrelated changes. Wait for findings, resolve
material issues, and rerun affected checks/review after substantive fixes.

Install with `agentics install --agent simplicity_reviewer`; Codex loads the
reviewer from `${CODEX_HOME:-~/.codex}/agents/` in a new session. If the client
cannot load custom agents, delegate the shared reviewer instructions and disclose
the fallback. This is an agent handoff requirement, not a Git or CI hook. See
[custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents).
