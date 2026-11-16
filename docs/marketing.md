# Public marketing pages

The Django application serves public pages about current capabilities, optional
configured analysis, and planned work, without a separate frontend build or
service. The [copy kit](marketing-copy.md) supplies reusable descriptions and
announcements with the same availability boundaries.

| Route | Purpose |
| --- | --- |
| `/` | Introduction, fictional workspace, core workflow, and self-hosting next step. |
| `/product/` | Current capabilities, fictional examples at `#analysis`, configured services, and input limits. |
| `/evidence/` | Complete evidence and analysis catalogue, supported imports/reports, input profiles, and setup requirements. |
| `/trust/` | Case access, originals, analysis approval, and map tile privacy. |
| `/roadmap/` | Available capabilities, planned work, and exploration without release dates. |
| `/hosting/` | Self-hosting and the planned hosted service. |

All pages are public. Signed-in `/` visitors go to **All cases**; other public
pages offer **Open workspace**. Authentication and email verification protect
case material, which public pages never query or display. Illustrations use
labelled fictional cases; examples retain source links, candidate-finding limits,
and configuration requirements. Trust explains [tile-provider privacy](maps.md#external-tiles-and-privacy).

Templates live in `marketing/templates/marketing/`; styling is
`marketing/static/marketing/marketing.css`. The editorial layout shares the
approved brand and [design standard](../DESIGN.md): prominent headings, readable
text, fine dividers, responsive/dark/forced-color layouts, keyboard focus, and a
skip link. Introductions lead to useful actions; separate pages own product,
trust, and planning details. Format limits and hosting questions use native
disclosures. Navigation and actions work without JavaScript.

## Evidence and analysis catalogue

**Evidence & analysis** groups every addable evidence type into its own section,
with analysis descriptions and requirements beside each method. Shared methods
appear in every applicable section. Pattern reports and supported import workflows
are listed separately from analysis. The same table content stacks at narrow
widths, retaining visible labels and table relationships.

`marketing/catalogue.py` owns the public presentation data, keyed by current
evidence-kind, analysis-operation, report, and import identifiers. It describes
supported capabilities rather than this installation's service readiness, and
public requests do not query case material or probe services. Keep it aligned with
the evidence/form registries, analysis operations, execution requirements, reports,
and their owning guides when capabilities change. Catalogue coverage tests detect
missing types/methods, incorrect source mappings, and changed model requirements;
review descriptions and input prerequisites against the implementation as well.

Requirements distinguish **Included**, **Included; AI optional**, **Private setup
or approved external AI**, and **Configured geographic service**. Included OCR
and plate reading use the standard Docker image's local tools. Private models and
geographic services need operator setup; external AI requires explicit approval
through Cloudflare AI Gateway, and external geographic requests have their own
approval. Private services do not imply external transfer. Runtime failure never
silently switches processing to a cloud service.

## Content boundaries

Hosted service is **Planned**: no launch date, pricing, waitlist, payment flow,
support commitment, certification, or availability guarantee is established.
Account routes access this installation; source publication does not deploy or
configure DNS for openpandect.com. Hosting copy does not replace live operator
configuration or legal policies.

Use **Available**, **Requires configuration**, **Planned**, and **Exploring**
consistently. Verify claims against [cases](cases.md), [evidence](evidence.md),
[analysis](analysis.md), and [maps](maps.md). Search is within a case. State the
bounded native PDF, printed-English JPEG/PNG, WAV, and import profiles rather
than implying every upload format is supported.

The public roadmap summarizes [proposed priorities](evidence-analysis-roadmap.md);
[analysis](analysis.md) owns implemented methods. Review public pages and the
copy kit when capabilities or priorities change.

Keep possible trips, plate candidates, identifier matches, and model suggestions
distinct from verified facts. Chronology shows recorded times separately from
uncertain extracted dates; briefs preserve source versions and reviews. Originals
and checksums establish integrity, not truth.

## Validation

Use [development PostgreSQL](development.md) and run sequentially to avoid test
database contention:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.tests config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.browser --noinput
```

[Browser validation](validation.md#browser-checks) owns engine installation,
artifacts, overrides, and limits. The marketing suite runs without JavaScript;
review synthetic captures against [DESIGN.md](../DESIGN.md#completion-and-design-review).
