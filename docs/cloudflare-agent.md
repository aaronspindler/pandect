# Cloudflare investigation Worker

Case Chat runs one Cloudflare Agents SDK Durable Object per turn. Pandect
dispatches each bounded step with opaque case and turn IDs and a short-lived
signed capability. The Worker chooses the next operation and requests an approved
model decision through Pandect's turn-scoped AI Gateway broker. The broker loads
verified excerpts and context, checks the current owner grant and caller access,
and accounts for provider usage. The Worker receives the proposed decision and
invokes its typed action. Only Pandect can publish a reply.

## Configure the private route

From `cloudflare/investigator`, use `npm ci`, `npm test` and `npm run check`
to install the pinned SDK, check the investigation protocol and bundle the
Worker locally. Configure an operator-owned HTTPS custom route for this Worker.
The Wrangler configuration disables workers.dev, preview URLs and observability.
Provide a random secret of at least 32 characters as `PANDECT_AGENT_SECRET` to
the Worker and Pandect's web and task services. Configure the Worker's
`PANDECT_ORIGIN` with the canonical HTTPS Pandect origin, without a trailing
slash. Set `PANDECT_AGENT_URL` on Pandect to the exact Worker endpoint ending
in `/step`. The case owner approves this URL in Case Settings. Case Chat also
needs the case's AI Gateway model and private AI Search configuration.

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
Each claim is checked against its quoted passage and assessed separately for
support. Claims that fail verification are withheld. Usage and search limits
produce a coverage-limited reply. A population review freezes the source
manifest and processes bounded batches. For an explicit quoted-term review,
the Agent searches eligible values in each authorized window. Pandect validates
the returned Unicode offsets against the frozen source and discloses unreadable
entries. Structured counts use case-scoped record fields. A proposed PDF extraction requires
investigator approval and resumes the same question using the original
cutoff plus that separately dated result.

Worker and Gateway errors mark the delivery as needing attention. Retry from
the reply to retain the cutoff and previously consumed budget. For a confirmed
interrupted queue delivery, use the existing `recover_evidence_chat` management
command with the reviewed delivery token. Stale callbacks cannot advance a new
delivery. Changing case permissions or the approved destination stops further
transfers.

Cloudflare documents server-side RPC through named Agent instances and Durable
Object bindings in its [Agent routing guide](https://developers.cloudflare.com/agents/agent-class/routing/).


For per-turn cost estimates, set `PANDECT_AI_PRICES_USD_PER_MILLION` to a
JSON object keyed by the approved `provider/model`. Each value is a pair of USD
prices per million input and output tokens. Pandect pins those rates at dispatch
and calculates the estimate from reported token counts. The usage panel identifies
unpriced requests. Estimates use ordinary input and output rates. Contract
discounts, cache pricing, storage and Worker charges appear on the provider bill.
