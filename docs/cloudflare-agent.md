# Cloudflare investigation Worker

Case Chat runs one Cloudflare Agents SDK Durable Object per turn. Pandect
dispatches each bounded step with opaque case and turn IDs and a short-lived
signed capability. The Worker chooses the next operation and requests an approved
model decision through Pandect's turn-scoped AI Gateway broker. The broker loads
verified excerpts and context, checks the current owner grant and caller access,
and accounts for provider usage. The Worker receives the proposed decision and
invokes its typed action. Only Pandect can publish a reply.

## Configure the private route

From `cloudflare/investigator`, run `npm ci`, `npm test` and
`npm run check` for the protocol tests and local dry-run bundle.
Configure an operator-owned HTTPS Worker route. Wrangler disables workers.dev,
preview URLs and observability.

Set the same random `PANDECT_AGENT_SECRET` of at least 32 characters on the
Worker and Pandect web and worker services. Set `PANDECT_ORIGIN` on the Worker
to Pandect's canonical HTTPS origin with no trailing slash. On Pandect, set
`PANDECT_AGENT_URL` to the exact Worker URL ending in `/step`. Evidence chat
also needs current owner approval, the AI Gateway model and private AI Search.

Worker callbacks require inbound HTTPS access to the Pandect web service at
`/cases/<case-uuid>/chat/agent/<turn-uuid>/`. Keep credentials, request and
response bodies out of proxy and Worker logs. The route requires a signed token
bound to the case, turn, delivery and approval revision. It rejects browser
cookies as authorization. Keep public Agent SDK routes and browser WebSockets
disabled. Durable Object RPC is invoked only after the Worker verifies Pandect's
service credential. The Agent stores no transcript, source text or answer in its
Durable Object. Pandect persists the canonical turn, exact source locators,
usage, decisions about proposed work and the processing cutoff.

## Investigation and recovery

Each question records the state of processing and search indexing at its evidence
cutoff. Indexed passages can support an answer while other passages are queued
or failed. The saved answer keeps that coverage notice after indexing recovers.

An ordinary turn may perform five material lookups. It searches, opens exact
passage offsets, follows cited originals or checks narrative relationships.
Explicit type and date filters apply to ordinary turns' context, lookups and
citations. A filtered turn leaves out prior reply prose and aggregate reports
whose full scope cannot be verified. Legacy entity notes stay out of new Chat
context. Previously published replies retain their saved provenance.
Each claim is checked against its quoted passage and assessed separately for
support. Claims that fail verification are withheld. Usage and search limits
produce a coverage-limited reply. A population review freezes the source
manifest and processes bounded batches. For an explicit quoted-term review,
the Agent searches eligible values in each authorized window. Pandect validates
the returned Unicode offsets against the frozen source and discloses unreadable
entries. New Chat context identifies a date-only message by its source calendar
date and shows that no time was supplied. Structured counts include it in
undated totals and disclose uncertainty when a count requires an event time. A proposed PDF extraction requires
investigator approval and resumes the same question using the original
cutoff plus that separately dated result.

Worker and Gateway failures need attention. Retry from the reply to keep its
cutoff and consumed budget. Inspect the delivery in the application environment.

```sh
python manage.py recover_evidence_chat TURN_UUID
python manage.py recover_evidence_chat TURN_UUID --confirm-interrupted DELIVERY_TOKEN
```

Use the second command after confirming that worker stopped. Stale callbacks
cannot advance a replacement delivery. Revoked case access or approval stops
further transfers.

See [Cloudflare Agent routing](https://developers.cloudflare.com/agents/agent-class/routing/).

For per-turn cost estimates, set `PANDECT_AI_PRICES_USD_PER_MILLION` to a
JSON object keyed by the approved `provider/model`. Each value is a pair of USD
prices per million input and output tokens. Pandect pins those rates at dispatch
and calculates the estimate from reported token counts. The usage panel identifies
unpriced requests. Estimates use ordinary input and output rates. Contract
discounts, cache pricing, storage and Worker charges appear on the provider bill.
