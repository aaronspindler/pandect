# Instagram monitor pilot

The local proof of concept reads follower and following lists visible to an
investigator in Instagram’s signed-in website. A separate offline fixture
exercises the same case workflow with generated lists. Collection stays off
by default. Existing synthetic captures retain their source labels.

## One controlled check on a Mac

Use the project’s locked development environment and its headed Chromium
installation. Run this from a private desktop on the pilot Mac. Substitute a
target covered by the pilot permission and a new output filename.

```sh
uv sync --locked
uv run --locked playwright install chromium
mkdir -p -m 700 data/validation/instagram-live
uv run --locked python -m instagram_pilot.qualify --target APPROVED_HANDLE --output data/validation/instagram-live/first.json
```

A new private browser opens on Instagram. Sign in directly there, complete
any two-factor check, then return to the terminal and press Enter. The
collector verifies the signed-in observer and target profile, then opens Followers
once to identify the numeric account and the website’s first-page request.
It reads the two JSON endpoints and follows each list’s pagination cursor.
The browser handles authentication. Automated collection actions are at least
five seconds apart. Each check has ten minutes to collect both directions.

The output includes the member handles and account IDs returned by the website,
pages read, exact profile count when available, and coverage for each direction.
A complete list requires an explicit end to pagination, consistent identities,
and agreement with any exact profile count. If the endpoint reaches its final
page with a different count, the capture records the mismatch and uses the
accounts Instagram returned for comparisons. Interrupted captures are
ineligible for comparison.
A challenge or rate limit stops the check. Use a fresh filename on a later
check to preserve the first observation. The command closes the temporary
browser session when finished. Keep its output in private storage.

For a second standalone check, pass the earlier capture as `--baseline`
and choose a new output file. Sign in with the same Instagram account.
The new JSON includes newly observed accounts for each comparable direction
and the coverage of both captures.

```sh
uv run --locked python -m instagram_pilot.qualify --target APPROVED_HANDLE --baseline data/validation/instagram-live/first.json --output data/validation/instagram-live/second.json
```

## Use the local browser with a Pandect test case

The web process, worker and optional browser service run on the same desktop
for this pilot. Set the following private environment variables identically
for all three. Leave the synthetic service URL empty. Apply the app’s additive
migrations to the local test installation using its normal release procedure.

| Setting | Value |
| --- | --- |
| `PANDECT_INSTAGRAM_LIVE_URL` | An unused explicit loopback address, e.g. `http://127.0.0.1:8765`. |
| `PANDECT_INSTAGRAM_LIVE_TOKEN` | A dedicated random URL-safe secret of 32 to 128 characters. |
| `PANDECT_INSTAGRAM_LIVE_USER`, `PANDECT_INSTAGRAM_LIVE_CASE` | UUIDs of the pilot investigator and case. |
| `PANDECT_INSTAGRAM_LIVE_TARGETS` | Comma-separated approved target handles, without `@`. |
| `PANDECT_INSTAGRAM_LIVE_OBSERVER` | Optional expected Instagram handle for the signing-in investigator. |
| `PANDECT_INSTAGRAM_LIVE_MIN_HOURS` | Minimum scheduled interval of 1, 6, 12, or 24 hours. Defaults to 1. |

Start the browser service from the checkout with the locked developer tools
and these settings available to the process.

```sh
uv run --locked --env-file .env python -m instagram_pilot.live_host
```

The service binds only to loopback. **Connect Instagram account** opens a
private Chromium window on the pilot Mac. Sign in on Instagram, then confirm
in Pandect within ten minutes. The browser holds the session for up to 48
hours from confirmation. A monitor started soon after sign-in can capture its
baseline and check again 24 hours later while the Pandect tab is closed.
Closing the browser, stopping the local service, or disconnecting ends access.
When the session expires, reconnect and resume the monitor to establish a new
baseline. Pandect stores only an opaque
service binding. A restart-safe browser credential vault is outside this
local proof of concept.

## Investigator workflow

1. Open **Case Tools → Instagram monitors** in the test case. Connect and
   confirm the observer through your personal connection settings.
2. Choose **Create monitor** and a check interval of 1, 6, 12, or 24 hours. The
   first check runs as soon as a worker is available. Later checks continue
   while your browser is closed.
3. Open the monitor to see the latest comparison date and additions for
   **New followers** and **New accounts followed**. A comparison with no new
   accounts says **No additions observed**. The monitor shows when a newer
   check failed or its capture could not be compared. The first complete list
   in either direction establishes its baseline.
   A live list that reaches the final page also qualifies when the profile
   count disagrees. Affected comparisons display the mismatch and show
   additions among the accounts Instagram returned.
   The fixture changes one account per list each hour while keeping its total
   constant. A follow and unfollow between checks can go unobserved.
4. Open a comparison to see its two captures. Expand **Check history** for
   earlier changes and all retained captures.
   Each original records the observer, target, direction, collection window,
   coverage, collector version, and observed names. When stable IDs are
   missing, the comparison describes newly observed handles. A rename can
   appear as a new handle.

The offline fixture traverses generated pages with delayed pagination and scroll-loaded
rows. It recognizes explicit list endings, checks the expected count, and keeps
partial captures when loading stalls or hits a limit. In the fixture, a complete
list advances its direction's baseline. All observations are shared with current
case members. Their originals identify the source, and live captures also record the profile URL.

The investigator who connects the account can use **Schedule** to pause their
monitor or change its interval, and can resume a paused monitor from its results.
The next check keeps its scheduled time unless the operator raised the minimum
and the existing delivery is too early. Later checks follow the new interval on
the same monitor. Earlier captures and comparisons stay available. The case
owner can also confirm a permanent stop. Viewers can read published observations.
The Account menu shows **Instagram connection** when the pilot is available to
the investigator or they have a retained connection.
Disconnecting in account settings pauses active monitors across the
investigator's cases. Starting a replacement live sign-in also pauses them.
Cancelling that sign-in leaves them paused. After reconnecting, resume each
monitor to establish a fresh comparison baseline.
Pausing, stopping, and disconnecting preserve evidence. Permanent source
deletion follows the [confirmed inventory](evidence.md#permanent-deletion) and
removes comparisons that cited the deleted capture.

## Enable the offline pilot

Use a test installation and apply the additive Django migrations through its
normal release procedure. Set these values for both web and worker in your
private environment. Generate a dedicated random key of at least 32 characters.

```dotenv
PANDECT_INSTAGRAM_PILOT_URL=http://instagram-pilot:8080
PANDECT_INSTAGRAM_PILOT_KEY=<dedicated-random-secret>
```

Start the optional service alongside the configured application:

```sh
docker compose --profile instagram-pilot up --build -d --wait
```

The service has a private Compose network, no published port, no database or
evidence volume, and accepts only an empty fixture request. The signing key
stays with web and worker. A synthetic pairing request lasts ten minutes, and
the resulting synthetic connection expires after 30 days. Rotating the key
requires investigators to reconnect.

**Processing** links to monitor history and recovery. A failed check retains
its safe reason. The monitor distinguishes ordinary running work from checks
overdue by more than the collection window plus a grace period. Ask the
operator to verify that the worker stopped before requesting recovery. A busy
browser defers work, and a prior generation
cannot publish or start a second schedule. To retire the pilot, stop the
monitors, clear both settings on web and worker, and stop the optional service.
Retained captures stay accessible under their case permissions.

## Live qualification

On November 17, 2026, opening each list on one controlled profile in the
signed-in website produced these requests in Chrome DevTools Network.

| Profile control | Request |
| --- | --- |
| Followers | `GET /api/v1/friendships/{numeric_account_id}/followers/` |
| Following | `GET /api/v1/friendships/{numeric_account_id}/following/` |

Both first pages returned HTTP 200 with a JSON `users` list, `has_more`, and
`next_max_id`. A subsequent controlled check followed both lists to their
terminal pages. The followers result matched its profile count. The following
result contained fewer distinct accounts than the displayed total. Each returned
member had a numeric account ID. The endpoint supplies no explanation for a
discrepancy between those two observations.

The service identifies this collector as `instagram-endpoints-v1`. Earlier
captures retain their original collector version. Each direction gets its own
baseline from the first capture that reaches a usable final page under this version.

Meta's Instagram API documents follower and following counts. We found no
documented endpoint for these named rosters. Meta requires express permission
for automated collection, including with a signed-in account.
[Instagram account API](https://developers.facebook.com/docs/graph-api/reference/shadow-ig-user)
and [Instagram terms](https://help.instagram.com/581066165581870/).

Live qualification uses controlled accounts covered by that permission.
Confirm both directions independently, including a known addition with
unchanged totals, and inspect the coverage and observer recorded in each
capture. Recheck sources when the website layout or observation method changes.
