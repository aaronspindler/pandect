# Pandect documentation

[PRINCIPLES.md](../PRINCIPLES.md) owns principles, [AGENTS.md](../AGENTS.md)
contributor requirements, and [DESIGN.md](../DESIGN.md) interface standards.
The guides below describe current behavior and procedures; the
[archive](archive/README.md) contains historical reports.

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
- [Instagram monitor pilot](instagram-monitors.md)
- [Evidence, communications, retention, and private downloads](evidence.md)
- [Vehicle locations and Asset Location CSV imports](vehicle-location-imports.md)
- [Analysis, reports, and external AI configuration](analysis.md)
- [Original image, document, and recording previews](original-previews.md)
- [Chronology](chronology.md)
- [Retained question-driven synthesis results](synthesis.md)
- [Local inference, model qualification, and private geographic services](local-analysis.md)
- [Geographic context and retained map references](analysis-geographic-context.md)
- [Investigation maps and location coverage](maps.md)

## Design and planning

- [Investigator screens and workflows](interface-design.md)
- [Current interface review and remaining coverage](interface-review.md)
- [Design references and assets](design/README.md)
- [Public marketing pages](marketing.md)
- [Project descriptions and reusable marketing copy](marketing-copy.md)
- [Analysis roadmap](evidence-analysis-roadmap.md)
- [Archived planning, reviews, and qualification reports](archive/README.md)

## Codex project configuration

[`.codex/config.toml`](../.codex/config.toml) supplies defaults to new trusted
sessions; session overrides and managed requirements may take precedence. See
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [configuration layering](https://learn.chatgpt.com/docs/config-file/config-basic).

Planning uses GPT-6 Astra/Ultra; implementation uses GPT-6.2 Sol/Extra high, both
with Fast enabled. Switch models explicitly: `plan_mode_reasoning_effort` sets
only effort. In the CLI, use `/model`, then `/plan`; reselect the implementation
model after leaving Plan mode. Availability depends on the client/account;
defaults do not change running conversations.

Automatic review handles eligible approval requests. Sandbox and shell network
restrictions still apply, including when live web search is enabled.

### Simplicity review

Use Agentics' [`simplicity_reviewer`](https://github.com/aaronspindler/agentics)
for the [required handoff review](../AGENTS.md#simplicity-and-maintainability).
It inherits parent model/reasoning and permissions and stays read-only. Supply
requirements, the final scoped diff (including new files), and Pandect safeguards;
exclude unrelated work. Wait for findings, fix material issues, and rerun affected
checks/review after substantive fixes.

Install with `agentics install --agent simplicity_reviewer`; start a new session
to load it from `${CODEX_HOME:-~/.codex}/agents/`. If custom agents cannot load,
delegate the shared instructions and disclose the fallback. Review is a handoff
requirement, not a Git/CI hook. See
[custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents).
