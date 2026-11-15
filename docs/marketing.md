# Public marketing pages

Pandect's public pages explain the self-hostable investigation workspace, show
what investigators can use today, and distinguish optional configured analysis
from planned work. They are served by the existing Django application, with no
separate frontend build or marketing service. The
[reusable copy kit](marketing-copy.md) supplies project descriptions and
announcement copy with the same availability boundaries.

| Route | Purpose |
| --- | --- |
| `/` | Plain-language introduction, fictional workspace, current capabilities, and planned-work signposting. |
| `/product/` | Available evidence, findings, exact connections, chronology, shared briefs, collaboration, map and import workflows; analysis examples at `#analysis`; separately configured services. |
| `/trust/` | Case access, original preservation, analysis approval, and map tile privacy. |
| `/roadmap/` | Available capabilities, planned work, and exploratory ideas without a release schedule. |
| `/hosting/` | Self-hosting and the planned hosted service. |

Anonymous visitors can read every page. Signed-in visitors opening `/` retain
the shortcut to **All cases**; the other public pages show **Open workspace**.
The existing sign-in and email-verification checks still protect case material.
Public pages never query or display an investigator's cases or evidence.
Illustrations use a labelled fictional case. Examples show source links and
candidate-finding limits; configuration-dependent analysis is identified beside
the claim. Trust includes [external tile-provider privacy](maps.md#external-tiles-and-privacy).

The pages use `marketing/templates/marketing/` and `marketing/static/marketing/marketing.css`, sharing
the approved brand artwork. Navigation and calls to action are ordinary links
and work without JavaScript. Responsive layouts, dark appearance, keyboard
focus, a skip link, and forced-colors brand text follow [the design
standard](../DESIGN.md).

## Content boundaries

The hosted service is described as planned. These pages do not establish a
launch date, pricing, a signup waitlist, payment handling, support commitments,
security certification, or availability guarantees. Existing account routes
belong to the running installation; they are not a hosted-service purchase flow.
Publishing these source changes does not configure DNS or deploy openpandect.com.

Use **Available**, **Requires configuration**, **Planned**, and **Exploring**
consistently. Keep product claims aligned with implemented behavior documented in
[cases](cases.md), [evidence](evidence.md), [analysis](analysis.md), and
[maps](maps.md). Search means search within a case. Native PDF text extraction,
printed-English JPEG/PNG recognition, and the supported WAV and import profiles
have specific coverage limits; avoid implying support for every uploaded format.

The public roadmap summarizes the [proposed priorities](evidence-analysis-roadmap.md).
Use that guide for future scope and the [current analysis guide](analysis.md) for
implemented behavior. Review the public pages and copy kit when capabilities ship
or priorities change.

Possible trips, plate readings, exact identifier matches, and model suggestions
stay distinguishable from verified facts. Chronology displays recorded times,
with uncertain extracted dates kept separate. Shared briefs preserve exact source
versions and review decisions. Original preservation and checksums describe
integrity, not truth.
Hosting copy does not replace operator configuration or legal policies for a live
service.

## Validation

Use the development PostgreSQL setup from [local development](development.md).
Run the tests sequentially so they do not compete for the same test database:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.tests.test_views config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.browser --noinput
```

Use [browser validation](validation.md#browser-checks) for engine installation,
artifact locations, overrides, and limits. The marketing suite exercises the
public pages without JavaScript. Review its synthetic captures against
[DESIGN.md](../DESIGN.md#completion-and-design-review).
