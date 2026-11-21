# Public marketing pages

The Django application serves public pages about current capabilities, optional
configured analysis, and planned work, without a separate frontend build or
service. The [copy kit](marketing-copy.md) supplies reusable descriptions and
announcements with the same availability boundaries.

| Route | Purpose |
| --- | --- |
| `/` | Combined product overview, core workflow, fictional examples at `#analysis`, and links to capabilities and GitHub documentation. |
| `/evidence/` | Complete evidence and analysis catalogue, supported imports/reports, input profiles, and setup requirements. |
| `/trust/` | Case access, originals, analysis approval, and map tile privacy. |
| `/roadmap/` | Planned work and exploration without release dates; linked from the footer. |

These four pages are public, including the overview for signed-in visitors. All
offer **Open workspace** when signed in; authentication still leads to **All cases**.
The former `/product/` route permanently redirects to `/`. Its `#available`,
`#analysis`, `#configured-services`, and `#limits` fragments remain valid on the
overview; service and format details now live in the catalogue. The primary
navigation contains Overview, Evidence & analysis, and Trust; Roadmap is
in the footer navigation, with an explicit current-page state. Product copy leads
with evidence, findings, and collaboration. The overview links to capabilities
and fictional examples; self-hosting remains a supporting documentation link in
the footer. The catalogue links to project and analysis guides. There is no
hosting marketing page.

Authentication and email verification protect case material, which public pages
never query or display. Illustrations use
labelled fictional cases; examples retain source links, candidate-finding limits,
and configuration requirements. Trust explains [tile-provider privacy](maps.md#external-tiles-and-privacy).

Templates live in `marketing/templates/marketing/`; styling is
`marketing/static/marketing/marketing.css`. The public pages use the technical
[marketing identity](../DESIGN.md#marketing-identity): graphite and teal surfaces,
large system-font headings, numbered sections, fine rules, and structured evidence
illustrations. The approved arch logo retains its oxblood/ivory artwork. Marketing
and investigator pages share palette, font, and control-radius tokens in
`ui/static/ui/primitives.css`; the public shell owns its layout, larger headings,
and fixed ink surfaces for fictional showcases. Investigator controls remain
owned by `ui/`.
All pages share responsive light/dark/forced-color layouts, keyboard focus, and a
skip link. Static showcases are labelled fictional, retain readable source and
uncertainty, and do not simulate interactive controls. The overview owns the
product story and examples; the catalogue owns
input profiles and method requirements; Trust owns access, retention, and
processing privacy; Roadmap owns future work. Setup and operator responsibilities
belong in the GitHub documentation. Keep short configuration and uncertainty notes
beside relevant claims without repeating full explanations. Navigation and actions
work without JavaScript.

## Evidence and analysis catalogue

**Evidence & analysis** leads with an analysis directory and evidence-family
navigation: All analysis, Documents & text, Images, Audio, Messages & calls, and
Locations. Ordinary `?type=` links filter methods, pattern reports, imports, and
input profiles without JavaScript. Unknown categories show the complete catalogue.
Messages & calls leads with session and timing tools before shared text/audio
methods. All analysis retains every supported method once.

Each method shows its outcome and short setup requirement. Native **Details**
disclosures retain precise inputs, configuration, uncertainty, and links to the
applicable input profiles. The supporting column explains intake and requirements;
**Supported formats & limits** retains a disclosure for every selected evidence
type. On narrow screens the supporting column follows the methods. A jump beside
the evidence-family navigation opens formats and limits directly, and each
method links to its inputs. Original method and evidence-kind fragment
identifiers remain available on the complete catalogue.

`marketing/catalogue.py` owns the public wording, keyed by current
evidence-kind, analysis-operation, report, and import identifiers. Analysis
input kinds and requirement labels derive from the static operation definitions
and inference rules. Public pages describe supported capabilities independently
of service readiness, without querying case material or probing services.
Catalogue tests verify coverage. Review descriptions and input prerequisites
against the owning guides when capabilities change.

Visible requirements distinguish **Included**, **AI optional**, **Requires AI
setup**, and **Geographic setup**. Details retain the complete setup descriptions,
including private services and explicitly approved external processing. Included OCR
and plate reading use the standard Docker image's local tools. Private models and
geographic services need operator setup. External AI requires explicit approval
for the configured destination, and external geographic requests have their own
approval. Runtime failure never switches processing to a cloud service.

## Content boundaries

Hosted service is **Planned**: no launch date, pricing, waitlist, payment flow,
support commitment, certification, or availability guarantee is established.
Account routes access this installation; source publication does not deploy or
configure DNS for openpandect.com. Public copy does not replace live operator
configuration or legal policies.

Use **Available**, **Requires configuration**, **Planned**, and **Exploring**
consistently. Verify claims against [cases](cases.md), [evidence](evidence.md),
[analysis](analysis.md), and [maps](maps.md). Search is within a case. State the
bounded native PDF, printed-English JPEG/PNG, WAV, and import profiles rather
than implying every upload format is supported. Message imports support the
documented iMessage CSV profile only; HTML message imports are unavailable.

The public roadmap summarizes [proposed priorities](evidence-analysis-roadmap.md);
[analysis](analysis.md) owns implemented methods. Review public pages and the
copy kit when capabilities or priorities change.

Keep possible trips, plate candidates, identifier matches, and model suggestions
distinct from verified facts. Chronology shows recorded times separately from
uncertain extracted dates. Results preserve source versions. Originals
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
