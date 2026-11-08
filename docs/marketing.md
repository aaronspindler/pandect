# Public marketing pages

Pandect's public pages introduce the investigation workspace and the planned
hosted service at **openpandect.com**. They are served by the existing Django
application, with no separate frontend build or marketing service.

| Route | Purpose |
| --- | --- |
| `/` | Product introduction and illustrative investigation workspace. |
| `/product/` | Evidence, case collaboration, and supported analysis workflows. |
| `/trust/` | Case access, original preservation, and responsible analysis. |
| `/roadmap/` | Available capabilities, next priorities, and exploratory ideas. |
| `/hosting/` | Self-hosting and the planned hosted service. |

Anonymous visitors can read every page. Signed-in visitors opening `/` retain
the shortcut to **All cases**; the other public pages show **Open workspace**.
The existing sign-in and email-verification checks still protect case material.
Public pages never query or display an investigator's cases or evidence. The home
page includes a fictional case preview that pairs captured text with suggested
keywords. Narrow layouts show the evidence reader first. Product describes the
implemented workflows, and Trust explains preservation, access, external AI
approval, and privacy when operators configure external map tiles.

The pages use `templates/marketing/` and `static/pandect/marketing.css`, sharing
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

Keep product claims aligned with implemented behavior documented in
[cases](cases.md), [evidence](evidence.md), and [analysis](analysis.md). The public
roadmap summarizes the [proposed analysis priorities](evidence-analysis-roadmap.md)
and separates them from what is available. Keep future items provisional and
review the roadmap when capabilities ship or priorities change. Original
preservation and checksums do not prove truth, and analysis suggestions require
review. Hosting copy does not replace operator configuration or legal policies for
a live service.

## Validation

Use the development PostgreSQL setup from [local development](development.md).
Run the tests sequentially so they do not compete for the same test database:

```sh
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.tests.test_marketing config.tests.test_product_access --noinput
uv run --locked --env-file .env python manage.py test --settings=config.test_settings config.marketing_browser --noinput
```

The browser suite uses the installed Chromium and WebKit engines, with
JavaScript disabled. It checks local links and fragment targets, public
navigation, keyboard skip/navigation links and FAQ disclosures, visible focus,
single page headings, loaded images, and horizontal reflow
at the five design viewport sizes and 200% text sizing. It captures dark and
reduced-motion appearances, plus forced colors in Chromium. Screenshots go to
ignored `data/validation/marketing-browser`; the existing
`PANDECT_BROWSER_ENGINES` and `PANDECT_BROWSER_ARTIFACTS` overrides apply.

Review the captures as part of delivery. Browser checks do not establish full
accessibility compliance or validate a live hosted deployment.
