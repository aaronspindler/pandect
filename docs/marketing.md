# Public marketing pages

The Django application serves public pages about current capabilities, optional
configured analysis, and planned work, without a separate frontend build or
service. The [copy kit](marketing-copy.md) supplies reusable descriptions and
announcements with the same availability boundaries.

| Route | Purpose |
| --- | --- |
| `/` | Combined product overview, core workflow, fictional examples at `#analysis`, and routes to capabilities and self-hosting. |
| `/evidence/` | Complete evidence and analysis catalogue, supported imports/reports, input profiles, and setup requirements. |
| `/trust/` | Case access, originals, analysis approval, and map tile privacy. |
| `/roadmap/` | Planned work and exploration without release dates; linked from the footer. |
| `/hosting/` | Self-hosting and the planned hosted service. |

These five pages are public, including the overview for signed-in visitors. All
offer **Open workspace** when signed in; authentication still leads to **All cases**.
The former `/product/` route permanently redirects to `/`. Its `#available`,
`#analysis`, `#configured-services`, and `#limits` fragments remain valid on the
overview; service and format details now live in the catalogue. The primary
navigation contains Overview, Evidence & analysis, Trust, and Hosting; Roadmap is
in the footer navigation, with an explicit current-page state.

Authentication and email verification protect case material, which public pages
never query or display. Illustrations use
labelled fictional cases; examples retain source links, candidate-finding limits,
and configuration requirements. Trust explains [tile-provider privacy](maps.md#external-tiles-and-privacy).

Templates live in `marketing/templates/marketing/`; styling is
`marketing/static/marketing/marketing.css`. The editorial layout shares the
approved brand and [design standard](../DESIGN.md): prominent headings, readable
text, fine dividers, responsive/dark/forced-color layouts, keyboard focus, and a
skip link. The overview owns the product story and examples; the catalogue owns
input profiles and method requirements; Trust owns access, retention, and
processing privacy; Hosting owns setup and operator responsibilities; Roadmap
owns future work. Keep short configuration and uncertainty notes beside relevant
claims without repeating full explanations. Hosting questions use native
disclosures. Navigation and actions work without JavaScript.

## Evidence and analysis catalogue

**Evidence & analysis** gives every addable evidence type a profile with its
intake, supported formats, prerequisites, and links to applicable tools. Separate
analysis, pattern report, and import tables describe each tool once, retaining
requirements and uncertainty notes alongside it. Each entry links back to its
applicable evidence types. The same table content stacks at narrow widths,
retaining visible labels and table relationships.

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
