# Public marketing pages

Django serves the public site, including for signed-in visitors. The public
navigation offers **Open workspace** when signed in. Authentication leads to
**All cases**. Public pages use fictional examples and never read case records.

| Route | Purpose |
| --- | --- |
| `/` | Overview, examples at `#analysis`, and setup and license at `#self-hosting`. |
| `/investigations/` | Workflow hub with incident, OSINT, communications, and vehicle/location detail pages. |
| `/evidence/` | Evidence and analysis catalogue with supported formats, limits, and prerequisites. |
| `/trust/` | Access, retention, processing choices, and map privacy. |
| `/roadmap/` | Proposed and exploratory work, linked from the footer. |

`/product/` permanently redirects to `/`. Preserve the overview's `#available`,
`#analysis`, `#configured-services`, and `#limits` anchors for existing links.
Templates and CSS live under `marketing/`. Use the shared UI tokens and follow
[marketing design standards](../DESIGN.md#marketing-identity).

## Investigation workflows

Link each detail page back to the hub, to the relevant catalogue category, and
to its owning guide. Label examples fictional and put uncertainty beside the
observation it qualifies.

- **Incidents.** Distinguish reported event time from collection time. [Chronology](chronology.md#reading-a-day)
- **OSINT.** Trace repeated identifiers to collected sources. Matching strings leave identity and ownership unresolved. [Case analysis](analysis.md)
- **Communications.** Check export timezones, participants, and coverage gaps. [Message CSV imports](analysis-communications.md#message-csv-imports)
- **Vehicle and location.** Trace possible trips to observations and imported rows. Sampling gaps leave route, driver, and continuous presence unresolved. [Automatic vehicle trips](analysis-locations.md#automatic-vehicle-trips)

## Search discovery and sharing

`PANDECT_PUBLIC_ORIGIN` sets canonical URLs, Open Graph URLs, and the sitemap
origin. Set it to `https://openpandect.com` for the official deployment.
`/sitemap.xml` lists the canonical public pages. Catalogue filters and tracking
parameters retain the base page's canonical URL.

The application serves a permissive `/robots.txt` with a sitemap link and adds
`X-Robots-Tag: noindex` to private routes. Case authorization protects private
material. Check deployed crawler access separately because Cloudflare can change
robots directives and enforce bot restrictions. [Cloudflare robots guidance](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/) · [OpenAI crawler guidance](https://developers.openai.com/api/docs/bots)

## Evidence and analysis catalogue

`marketing/catalogue.py` owns public catalogue wording by evidence kind,
operation, report, and import. Method kinds and inference requirements come from
operation definitions. `?type=` filters work without JavaScript. Keep each
method's outcome and setup visible, with inputs, limits, and uncertainty in
**Details**. Preserve method and evidence-kind anchors on the complete catalogue.

Use **Included**, **AI optional**, **Requires AI setup**, and **Geographic setup**
according to each method's requirements. Compare copy with the owning guide and
catalogue tests when capabilities change.

## Content boundaries

Describe supported features separately from optional setup and planned work.
Case Search uses a private Cloudflare index per enabled case. **Search evidence
across cases** queries eligible cases the investigator can access. Describe
coverage and link to the original sources. [Cloudflare AI Search](cloudflare-search.md)

Preserve uncertainty in descriptions of candidate trips, plate readings,
identifier matches, inferred sessions, and model suggestions. See the owning
guides for evidence, access, and analysis claims. [Evidence](evidence.md) · [Cases](cases.md) · [Analysis](analysis.md) · [Maps](maps.md)

Managed hosting is planned, with no published launch date or pricing. Keep the
public roadmap and [reusable copy](marketing-copy.md) aligned with the
[analysis roadmap](evidence-analysis-roadmap.md) and implemented workflows.

## Validation

Run sequentially against the [development PostgreSQL test setup](development.md).

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.tests config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings marketing.browser --noinput
```

The browser suite runs without JavaScript. Review captures using the
[browser validation guide](validation.md#browser-checks) and
[design review](../DESIGN.md#completion-and-design-review).

After release, verify public pages, `/sitemap.xml`, and `/robots.txt` through
the HTTPS edge. Check status, canonical URLs, and effective crawler access.
Operators with verified site access can submit the sitemap and track index and
search coverage. [Google Search Console](https://search.google.com/search-console/) · [Bing Webmaster Tools](https://www.bing.com/webmasters/)
