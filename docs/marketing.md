# Public marketing pages

The Django application serves public pages about current capabilities, optional
configured analysis, and planned work, without a separate frontend build or
service. The [copy kit](marketing-copy.md) supplies reusable descriptions and
announcements with the same availability boundaries.

| Route | Purpose |
| --- | --- |
| `/` | Product overview, case workflow, investigation hub link, fictional examples at `#analysis`, and setup and license links at `#self-hosting`. |
| `/investigations/` | Four investigation workflows and links to their detail pages. |
| `/investigations/incidents/` | Review incident records and distinguish event time from collection time. |
| `/investigations/osint/` | Organize collected public sources and inspect repeated identifiers. |
| `/investigations/communications/` | Review imported messages, call records, and supplied transcripts. |
| `/investigations/vehicle-location/` | Review location observations, supported CSV imports, and possible trips. |
| `/evidence/` | Complete evidence and analysis catalogue, supported imports/reports, input profiles, and setup requirements. |
| `/trust/` | Case access, originals, analysis approval, and map tile privacy. |
| `/roadmap/` | Planned work and exploration without release dates; linked from the footer. |

These pages are public, including the overview for signed-in visitors. All
offer **Open workspace** when signed in; authentication still leads to **All cases**.
The former `/product/` route permanently redirects to `/`. Its `#available`,
`#analysis`, `#configured-services`, and `#limits` fragments remain valid on the
overview; service and format details now live in the catalogue. The primary
navigation contains Overview, Investigations, Evidence & analysis, and Trust.
The footer also links to Roadmap. Product copy leads with evidence, findings,
and collaboration. The overview links to capabilities, investigations, and
fictional examples, with self-hosting setup and licensing in a supporting section.
The catalogue links to the relevant analysis and processing guides.

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
uncertainty, and do not simulate interactive controls. The overview introduces
the product. Investigation pages show how to use it for specific workflows.
The catalogue describes supported inputs and method requirements. Trust covers
access, retention, and processing privacy. Roadmap covers future work. Setup and
operator responsibilities belong in the GitHub documentation. Keep short
configuration and uncertainty notes beside relevant claims. Navigation and
actions work without JavaScript.

## Investigation workflows

The Investigations hub links to each detail page, and each breadcrumb leads back
to the hub. Detail pages explain the workflow, illustrate it with a labelled
fictional example, and link to the applicable evidence formats and source guide.
Show the remaining uncertainty beside the observations.

- **Incidents.** Distinguish the time reported for an event from the time a
  source was collected. [Chronology](chronology.md#reading-a-day)
- **OSINT.** Use investigator-collected public sources. A repeated identifier
  shows a matching string, while identity, ownership, and independent
  corroboration remain unresolved. [Case analysis](analysis.md)
- **Communications.** Use supported CSV exports and review the stated timezone,
  participants, and gaps. Treat intent as unresolved when the records only show
  reported messages and call activity. [Message CSV imports](analysis-communications.md#message-csv-imports)
- **Vehicle and location.** Trace possible trips to reported observations and
  imported rows. Sampling gaps leave the route, driver, and continuous presence
  unresolved. [Automatic vehicle trips](analysis-locations.md#automatic-vehicle-trips)

## Search discovery and sharing

Each page opens with a descriptive heading and a direct answer. Its unique
title and description also supply its Open Graph preview alongside the Pandect
icon. The homepage names Pandect with `WebSite` microdata.

`PANDECT_PUBLIC_ORIGIN` supplies the origin for canonical URLs, sharing
metadata, and the sitemap. Set it to `https://openpandect.com` on the official
deployment. `/sitemap.xml` lists the nine public landing pages above once each
at their canonical URLs. Catalogue filters and tracking parameters use their
base page's canonical URL.

`/robots.txt` advertises the sitemap and allows crawling by default so search
crawlers, including OAI-SearchBot, can reach public pages. Django sends
`X-Robots-Tag: noindex` on account, case, invitation, and admin responses.
Authorization protects private material independently of indexing directives.
Manage training crawler preferences separately at the edge. Cloudflare can
modify the robots response and block crawler requests independently of the
application. [Cloudflare robots guidance](https://developers.cloudflare.com/bots/additional-configurations/managed-robots-txt/)
· [OpenAI crawler guidance](https://developers.openai.com/api/docs/bots)

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
including the approved Cloudflare services for search, Case Chat, OCR, plate
reading and transcription. Native text extraction and deterministic checks run
in Pandect. Geographic services retain their own destination and approval settings.

## Content boundaries

Hosted service is **Planned**: no launch date, pricing, waitlist, payment flow,
support commitment, certification, or availability guarantee is established.
Account routes access this installation; source publication does not deploy or
configure DNS for openpandect.com. Public copy does not replace live operator
configuration or legal policies.

Use **Available**, **Requires configuration**, **Planned**, and **Exploring**
consistently. Verify claims against [cases](cases.md), [evidence](evidence.md),
[analysis](analysis.md), and [maps](maps.md). Search is within a case.
**Case Search** uses the private Cloudflare AI Search instance enabled by the
case owner. Explain searchable material, processing coverage and how to open
the original. See [hosted search](cloudflare-search.md).

The public roadmap summarizes [proposed priorities](evidence-analysis-roadmap.md);
[analysis](analysis.md) owns implemented methods. Review public pages and the
copy kit when capabilities or priorities change.

Keep possible trips, plate candidates, identifier matches, inferred sessions, and
model suggestions distinct from verified facts. Chronology shows recorded times
separately from uncertain extracted dates. Results preserve source versions.
Originals and checksums establish the retained content and its integrity.

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

After an authorized release, fetch the nine public pages, `/sitemap.xml`, and
`/robots.txt` through the public HTTPS edge. Check for 200 responses, matching
canonical and sitemap URLs, and effective search crawler access, including for
OAI-SearchBot. With verified site access, submit the sitemap in
[Google Search Console](https://search.google.com/search-console/) and
[Bing Webmaster Tools](https://www.bing.com/webmasters/). Use the first 28 days
to record a baseline of indexed pages, impressions, clicks, and available
generative search visibility metrics.
