# Project descriptions and reusable marketing copy

Descriptions for repository summaries, introductions, and announcements. Verify
against current implementation before reuse. [Marketing](marketing.md) owns the
public site; [analysis](analysis.md), [evidence](evidence.md), and domain guides
own feature details and supported inputs.

## One line

Pandect is open-source investigation software for organizing evidence in private
cases, searching case material, and tracing findings to their sources with
collaborators.

## Short description

Bring gathered material into a private case. Pandect keeps original files,
messages, and location observations together, helps you search and examine them,
and links findings to sources. Invite viewers or editors to review the case
together. Pandect is self-hostable and in active development.

## Project introduction

Pandect is an investigation workspace built with Django and
PostgreSQL. Organize collected material, including open-source intelligence,
in private cases with their own people, vehicles, evidence, and permissions.

Retain documents, images, audio, text, communications, and location observations.
Search supported case text and explore source-linked findings. Eligible evidence
receives automatic analysis, including native document text extraction. The case owner can enable
Cloudflare to recognize text in images. Maps, supported imports, exact identifier
connections, and recorded chronology help examine activity. Inspect supported
originals privately. Invite collaborators to inspect or edit.

The case owner enables Cloudflare search, transcription and Case Chat in Case
Settings. Chat can follow candidate relationships from statements and messages
back to the passages that support them. Inferred event timelines remain planned. Pandect is in active development. Self-hosting is available, and
managed hosting is planned.

## Announcement copy

Pandect brings evidence into a private investigation workspace.
Organize files, messages, and locations. Search a case and inspect sources behind
findings with collaborators. Pandect extracts supported document text and metadata. Cloudflare can recognize
printed text when enabled by the case owner. Explore identifier matches,
recorded chronology, and possible trips. Transcription and model suggestions
need configuration. Pandect is in active development, with inferred timelines,
graphs, and broader discovery planned.
[Source and setup](https://github.com/aaronspindler/pandect).

## Short social post

Pandect is an investigation workspace. Organize evidence in private
cases, browse source-linked findings, and follow recorded chronology.
Model-assisted analysis needs configuration. Pandect is in active development.
[Explore Pandect](https://github.com/aaronspindler/pandect).

## Investigation workflows

- **Incident investigations.** Review reports, photos, and timed records in a
  private case. Use recorded chronology to distinguish reported event times
  from collection times.
- **Open-source investigations (OSINT).** Organize public material you collect,
  retain its source URLs, and follow repeated identifiers back to the originals.
- **Communications investigations.** Review supported iMessage and WhatsApp CSV
  exports alongside call records and supplied transcripts. Check export timezones
  and gaps before interpreting the sequence.
- **Vehicle and location investigations.** Review reported observations and
  trace possible trips back to their source records and imported rows.

Use the workflow notes and source links in the marketing guide when adapting
this copy. [Investigation workflows](marketing.md#investigation-workflows)

## Editorial boundaries

Keep availability beside claims in all formats. Follow the
[public content boundaries](marketing.md#content-boundaries) for capability,
configuration, uncertainty, and roadmap wording.

- Lead with gathering material, organizing cases, searching, inspecting sources,
  and collaborating. Use **Findings** for the case destination.
- Use descriptive page headings and answer the reader's question in the opening
  paragraph. Keep page descriptions specific to the visible content.
- Link to setup and the AGPL-3.0-only license when describing self-hosting.
- Distinguish manually recorded social accounts from the offline
  [Instagram monitoring pilot](instagram-monitors.md). Describe active
  collection as a synthetic workflow and preserve the live release boundary.
- Label fictional or synthetic previews. Show controls only for available workflows.
- Use the project's license wording in reusable copy.
  [License wording](../README.md#license) · [LICENSE](../LICENSE)
