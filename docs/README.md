# Pandect documentation

[PRINCIPLES.md](../PRINCIPLES.md) owns principles, [AGENTS.md](../AGENTS.md)
contributor requirements, and [DESIGN.md](../DESIGN.md) interface standards.
The guides cover current behavior and procedures. Dated reports live in the
[archive](archive/README.md).

## Setup and operation

- [Development, configuration, and uv](development.md)
- [Architecture, runtime versions, app ownership, and migration baseline](architecture.md)
- [Background workers, diagnostics, and recovery](background-tasks.md)
- [Operations, storage, backup, and recovery](operations.md)
- [Dokploy deployment](operations.md#first-dokploy-deployment)
- [Hosted request activity and operator searches](request-activity.md)
- [Validation, browser checks, CI, and Dependabot](validation.md)

## Investigation workflows

- [Cases, invitations, permissions, and scoped administration](cases.md)
- [People](people.md) and [vehicles](vehicles.md)
- [Instagram monitor pilot](instagram-monitors.md)
- [Evidence, communications, retention, and private downloads](evidence.md)
- [Vehicle locations and Asset Location CSV imports](vehicle-location-imports.md)
- [Analysis, reports, and external AI configuration](analysis.md), with focused
  guides for [extraction](analysis-extraction.md),
  [communications](analysis-communications.md), and [locations](analysis-locations.md)
- [Original image, document, and recording previews](original-previews.md)
- [Chronology](chronology.md)
- [Evidence chat and cited answers](analysis.md#evidence-chat)
- [Retained question-driven synthesis results](synthesis.md)
- [Cloudflare AI Search](cloudflare-search.md) · [Cloudflare investigation Agent](cloudflare-agent.md) · [Model routes](local-analysis.md)
- [Geographic context and retained map references](analysis-geographic-context.md)
- [Investigation maps and location coverage](maps.md)

## Design and planning

- [Investigator screens and workflows](interface-design.md)
- [Interface review coverage and archived results](interface-review.md)
- [Design references and assets](design/README.md)
- [Public marketing pages](marketing.md)
- [Project descriptions and reusable marketing copy](marketing-copy.md)
- [Analysis roadmap](evidence-analysis-roadmap.md)

## Codex project configuration

Trusted sessions read [`.codex/config.toml`](../.codex/config.toml) for project
defaults. Use GPT-6 Astra with Fast for planning and the configured implementation
model with Fast for coding. Select the model explicitly when switching modes
because `plan_mode_reasoning_effort` controls effort only. See
[configuration layering](https://learn.chatgpt.com/docs/config-file/config-basic)
and the [settings reference](https://learn.chatgpt.com/docs/config-file/config-reference).

### Simplicity review

Install the Agentics reviewer with `agentics install --agent simplicity_reviewer`
and start a new session to load it from `${CODEX_HOME:-~/.codex}/agents/`. Invoke
it read-only with the final scoped diff and task requirements. If the custom
agent cannot load, delegate its shared instructions and report the fallback.
Follow the [handoff requirements](../AGENTS.md#simplicity-and-maintainability).
See [custom-agent setup](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents).
