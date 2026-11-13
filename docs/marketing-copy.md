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
imports, and reports help you examine recorded activity. Collaborators can review
the case or contribute as editors.

Transcription and model-assisted suggestions need separately configured services.
External AI requires deliberate selection and approval, with no automatic cloud
fallback. Pandect is in active development: unified timelines, relationship
exploration, and case-wide synthesis are planned. Self-hosting is available;
managed hosting is planned.

## Announcement copy

Pandect brings gathered evidence into a private investigation workspace you can
self-host. Organize files, messages, and locations; search within a case; and
inspect the sources behind findings with your collaborators. Local checks cover
supported document text, printed-English image recognition, metadata, and more.
Transcription and model-assisted analysis need additional configuration. The
project is in active development, with unified timelines and relationship tools
on the roadmap. Explore the source and setup guide on
[GitHub](https://github.com/aaronspindler/pandect).

## Short social post

Pandect is a self-hostable investigation workspace. Organize evidence in private
cases, search supported text, and review findings with links to their sources.
In active development; timelines and relationship tools are planned.
[Explore Pandect](https://github.com/aaronspindler/pandect).

## Feature language

Keep availability beside the claim, including in slides, screenshots, and short
announcements. Use **Available**, **Requires configuration**, **Planned**, and
**Exploring** consistently.

| Status | Describe as | Supporting reference |
| --- | --- | --- |
| Available | Private cases, owner-managed email invitations, viewer/editor collaboration, people and vehicle records. | [Cases](cases.md), [people](people.md), [vehicles](vehicles.md) |
| Available | Original documents, images, audio, captured text, messages, calls, and location observations with source details and private downloads. | [Evidence](evidence.md) |
| Available | Search within a case across retained text and supported extraction; source-linked findings and processing history. | [Analysis](analysis.md) |
| Available | Local native PDF/UTF-8 text extraction, JPEG/PNG metadata and printed-English recognition, possible plate readings, supported WAV metadata, and rule-based identifiers/dates/keywords. Format and coverage limits apply. | [Extraction](analysis-extraction.md), [text analysis](analysis-communications.md) |
| Available | Supported Asset Location CSV and iMessage HTML imports; possible vehicle trips, call/coverage/duplicate reports, and selected text, conversation, and location comparisons. | [Vehicle imports](vehicle-location-imports.md), [communications](analysis-communications.md), [locations](analysis-locations.md) |
| Available | Map recorded coordinates and person-linked points of interest; the map background uses an operator-configured tile provider. | [Maps](maps.md) |
| Requires configuration | English WAV transcription, model-assisted topics/claims, conversation summaries, and semantic conversation comparisons through a private runtime or separately selected and approved external AI route. | [Local analysis](local-analysis.md) |
| Requires configuration | Address lookup and geographic reference context using configured geographic services. External lookups require approval unless the operator designates the endpoint as local. | [Locations](analysis-locations.md), [geographic context](analysis-geographic-context.md) |
| Planned | Unified case timeline, candidate links across records, relationship exploration, and source-cited case-wide synthesis. Search across accessible cases is a broader product goal. | [Roadmap](evidence-analysis-roadmap.md), [project overview](../README.md#planned-and-exploratory) |
| Exploring | Semantic passage retrieval, near-duplicate image comparison, structured table extraction, and broader specialist analysis. | [Catalogue](evidence-analysis.md) |
| Planned | Managed hosting, with timing and pricing unannounced. | [Public hosting page](../marketing/templates/marketing/hosting.html) |

## Editorial boundaries

- Lead with the investigation task: gather material, organize a case, search,
  inspect sources, and collaborate. Use **Findings** for the case destination.
- Describe imports by their supported export profile. Recording a social account
  is manual record keeping; live account collection and vendor API integrations
  are outside the current feature set.
- Keep configuration requirements beside transcription, model assistance,
  geographic lookup, and map backgrounds. Core case work remains usable without
  AI credentials. OCR runs locally and never uses an external fallback.
- Distinguish possible trips, candidate plate readings, inferred sessions, and
  model suggestions from verified facts. Original preservation and checksums
  describe retained content and integrity, not truth or identity.
- Treat the roadmap as provisional. Avoid release dates, implied completion
  percentages, prices, hosted signup promises, guarantees, certifications,
  endorsements, and accuracy claims without supporting evidence.
- Use fictional cases and synthetic material in previews. Label illustrations
  and avoid showing future controls as part of the available workspace.
- Keep licensing wording aligned with the [license section](../README.md#license)
  and [LICENSE](../LICENSE); product copy does not introduce new legal terms.
