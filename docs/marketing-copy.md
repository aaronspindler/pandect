# Project descriptions and reusable marketing copy

Use these descriptions for repository summaries, project introductions, and
announcements. They describe the current application; review them against the
implementation before reuse. The public site is documented in
[marketing](marketing.md). Feature details and supported inputs belong in
[case analysis](analysis.md), [evidence](evidence.md), and the other domain guides.

## One line

Pandect is a self-hostable investigation workspace for organizing evidence,
searching within a case, and reviewing source-linked findings with collaborators.

## Short description

Bring the material you have gathered into one private case. Pandect keeps original
files, messages, and location observations together, helps you search and examine
them, and links findings back to their sources. Run it on your own infrastructure
and invite collaborators as viewers or editors. Pandect is in active development.

## Project introduction

Pandect is a self-hostable investigation workspace built with Django and
PostgreSQL. It helps investigators organize collected material, including
open-source intelligence, into private cases with their own people, vehicles,
evidence, and access permissions.

Today, you can retain original documents, images, audio, text, communications,
and location observations; search supported text within a case; and inspect
source-linked findings. Eligible evidence receives local checks, including native
document text extraction and printed-English image recognition. Maps, supported
imports, exact identifier connections, and structured chronology help you examine
recorded activity. Browse all retained findings, inspect supported originals in
private readers, and share exact selections as printable briefs with provenance.
Collaborators can review the case or contribute as editors.

Transcription and model-assisted suggestions, including synthesis over explicitly
selected text and findings, need separately configured services.
External AI requires deliberate selection and approval, with no automatic cloud
fallback. Pandect is in active development: inferred event timelines, relationship
graphs, and broader evidence discovery are planned. Self-hosting is available;
managed hosting is planned.

## Announcement copy

Pandect brings gathered evidence into a private investigation workspace you can
self-host. Organize files, messages, and locations; search within a case; and
inspect the sources behind findings with your collaborators. Local checks cover
supported document text, printed-English image recognition, metadata, and more.
Explore exact identifier matches, recorded chronology, and shared selections with
printable briefs. Transcription and synthesis over selected text/findings need
additional configuration. The project is in active development, with inferred
event timelines, graphs, and broader discovery on the roadmap. Explore the source
and setup guide on
[GitHub](https://github.com/aaronspindler/pandect).

## Short social post

Pandect is a self-hostable investigation workspace. Organize evidence in private
cases, browse source-linked findings, follow recorded chronology, and share cited
briefs. In active development; model-assisted analysis needs configuration.
[Explore Pandect](https://github.com/aaronspindler/pandect).

## Feature language

Keep availability beside each claim, including in slides, screenshots, and short
announcements. Follow the [content boundaries](marketing.md#content-boundaries)
and check the linked domain guides before reusing capability-specific copy.

## Editorial boundaries

- Lead with the investigation task: gather material, organize a case, search,
  inspect sources, and collaborate. Use **Findings** for the case destination.
- Describe imports by their supported export profile. Recording a social account
  is manual record keeping; live account collection and vendor API integrations
  are outside the current feature set.
- Keep configuration requirements beside transcription, model assistance,
  geographic lookup, and map backgrounds. Core case work remains usable without
  AI credentials. OCR runs locally and never uses an external fallback.
- Distinguish possible trips, candidate plate readings, exact identifier matches,
  inferred sessions, and model suggestions from verified facts. Recorded chronology
  does not verify an event, and shared selections do not accept findings. Original
  preservation and checksums describe retained content and integrity, not truth
  or identity.
- Treat the roadmap as provisional. Avoid release dates, implied completion
  percentages, prices, hosted signup promises, guarantees, certifications,
  endorsements, and accuracy claims without supporting evidence.
- Use fictional cases and synthetic material in previews. Label illustrations
  and avoid showing future controls as part of the available workspace.
- Keep licensing wording aligned with the [license section](../README.md#license)
  and [LICENSE](../LICENSE); product copy does not introduce new legal terms.
