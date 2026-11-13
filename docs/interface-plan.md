# Interface delivery and acceptance

The core interface is implemented. This document guides follow-up delivery;
[screen specifications](interface-design.md) describe current workflows and
[DESIGN.md](../DESIGN.md) owns the shared design requirements. Validation results
must be reported separately from implementation status.

## Current workflow refinement

The main workflow is **add evidence → automatic checks → Findings**. Keep routine
results visible and disclose manual configuration, reruns, processing history,
and reviews on request. Evidence readers show the original and Findings together,
with supporting details/history in header **Info** and permitted actions in
**More**. Additional analysis separates choosing a task from selecting its
sources and required context. See [case analysis](analysis.md) for supported
operations.

## Follow-up delivery

1. Inspect the affected workflow and its domain documentation. Preserve case
   roles, retained originals, source links, uncertainty, and deliberate external
   processing approval.
2. Use templates and static assets in the owning app. Shared UI belongs to `ui`,
   account/allauth overrides to `accounts`, and public pages to `marketing`.
   Follow the existing Django forms and progressive-enhancement patterns.
3. Make the full-page form or canonical record URL complete before adding richer
   interaction. Preserve safe return URLs, collection filters, browser Back,
   entered values after errors, and keyboard access.
4. Keep long processing in the existing bounded worker workflow. Original file
   uploads remain sequential requests with visible per-file outcomes; uncertain
   confirmation must resolve the same submission before editing or resubmitting.
5. Update the owning user/operator documentation in the same change. Put new
   proposals in the [analysis roadmap](evidence-analysis-roadmap.md), separate
   from claims about available behavior.

## Verification gates

Use [validation](validation.md) for executable commands and prerequisites.
Choose checks for the actual change:

- Behavior changes need PostgreSQL-backed tests for relevant case roles,
  unrelated users and superusers, revoked access, cross-case identifiers, CSRF,
  and private downloads. Exercise rollback, storage failure, stale forms, and
  idempotent retries where affected.
- Visible changes need the viewport, keyboard, appearance, reflow, and recovery
  checks in [DESIGN.md](../DESIGN.md#completion-and-design-review), with relevant
  synthetic screenshots. Preserve the evidence-list density targets without
  clipping names or shrinking controls.
- Asset packaging or container changes need the isolated Compose smoke workflow.
  Dependency changes need the locked dependency and security checks.
- Documentation-only changes need content, links, and whitespace review rather
  than application tests that restate prose.
- Complete the required [simplicity review](README.md#simplicity-review).
  Report local checks, browser inspection, hosted CI, and deployed behavior as
  separate evidence; a delivery plan does not establish that any check passed.
