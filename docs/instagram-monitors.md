# Instagram monitors

The offline pilot exercises case approval, personal connections, scheduled
checks, and evidence retention with generated follower and following lists.
Collection is off by default. Live scheduling is disabled pending a qualified,
Meta-authorized source of named rosters. Historical observations remain
available to current case members.

## Enable the offline pilot

Use a test installation. Set the following in its private Compose environment.
Generate a dedicated random key of at least 32 characters. Remove any legacy
`PANDECT_INSTAGRAM_LIVE_*` settings before switching from the desktop pilot.

```dotenv
PANDECT_INSTAGRAM_PILOT_URL=http://instagram-pilot:8080
PANDECT_INSTAGRAM_PILOT_KEY=<dedicated-random-secret>
```

```sh
COMPOSE_PROFILES=instagram-pilot make run
```

Use the same profile on subsequent rebuilds so `make run` waits for both workers
to finish active tasks before replacing their shared image. If the installation
uses other profiles, include them in the comma-separated `COMPOSE_PROFILES`
value. See [local startup](development.md#first-run).

The profile starts the fixture and `instagram-worker`. The dedicated worker
uses the same application image, PostgreSQL database, and private evidence
volume as the web and ordinary worker. It consumes the `instagram` queue so an
in-progress roster check leaves ordinary evidence jobs able to run. The fixture
has a private Compose network, no published port, and no database or evidence
volume. It accepts an empty request and returns generated lists. Connection
signing material stays with the application processes.

On upgrade, the additive migration pauses earlier schedules and keeps their
captures and comparisons. To resume a synthetic monitor, the case owner must
approve the target. Resume from the monitor detail to establish a new baseline
bound to the fixture account ID.

## Use a synthetic monitor

1. As case owner, approve the synthetic target **@demo.account** in **Case Settings**.
2. Open **Case Tools → Instagram monitors**. As a verified owner or editor,
   connect and confirm your own synthetic observer through personal account
   settings. Pairing lasts ten minutes. The resulting connection expires after
   30 days. Rotating the signing key requires reconnecting.
3. Create a monitor for the approved target. The first check runs as soon as
   the worker is available. The default interval is 24 hours. The fixture also
   supports 1, 6, and 12 hours. Checks continue while the Pandect tab is closed.
4. Open the monitor to see the latest check and its next scheduled check. Review
   **New followers** and **New accounts followed** separately. A quiet comparison
   says **No additions observed**. Each direction starts comparing after its
   first complete baseline. The fixture changes one account per list each
   synthetic hour while keeping its total constant.
5. Open a comparison to see its two captures. **Check history** lists earlier
   comparisons and retained captures. Each original records the observer,
   target, direction, collection window, coverage, collector version, and
   observed accounts. Current case members can read the saved results.

The fixture exercises generated pages with delayed pagination and scroll-loaded
rows. It retains partial captures when loading stalls or reaches a limit.
Only a complete fixture list advances that direction's baseline.

## Recover or turn off monitoring

The investigator who created a monitor can use **Schedule** to pause it, adjust
the interval, or resume. An interval change applies after the next scheduled
check. Disconnecting pauses the investigator's monitors across cases. Reconnect
and resume to establish a fresh baseline. Case owners can stop individual
monitors or revoke target approval in **Case Settings**. Revocation pauses the
affected monitors. Reapproval allows the investigator to resume them.

**Processing** links to monitor history and recovery. If a check is overdue,
ask the operator to verify that the worker stopped before requesting recovery.
A stale delivery cannot publish results or start a second schedule.

To turn off collection for the installation, let both workers finish their
active tasks before stopping the fixture and refreshing application settings.

```sh
docker compose --profile instagram-pilot stop --timeout -1 worker instagram-worker
docker compose --profile instagram-pilot stop instagram-pilot
# Remove PANDECT_INSTAGRAM_PILOT_URL and PANDECT_INSTAGRAM_PILOT_KEY from .env.
# Remove instagram-pilot from COMPOSE_PROFILES if it is set.
docker compose up -d --wait web worker
```

Pausing, stopping, disconnecting, and revoking approval preserve evidence.
Permanent source deletion follows the [confirmed inventory](evidence.md#permanent-deletion)
and removes comparisons that cited the deleted capture.

## One controlled check on a Mac

The standalone qualification command uses Instagram's signed-in website and
writes a private local capture. Use controlled accounts and targets covered by
Meta's authorization for the automated method. Run from a private desktop with
the project's locked development environment and headed Chromium.

```sh
uv sync --locked
uv run --locked playwright install chromium
mkdir -p -m 700 data/validation/instagram-live
uv run --locked python -m instagram_pilot.qualify --target APPROVED_HANDLE --output data/validation/instagram-live/first.json
```

Sign in directly in the private Instagram window and complete any verification.
Return to the terminal and press Enter. The collector verifies the observer and
target, then follows each list to its final page within the collection window.
A challenge or rate limit stops the check. The command closes the temporary
browser session on completion. Keep its output in private storage.

For a second standalone check, use the same observing account, pass the earlier
capture as `--baseline`, and choose a new output file.

```sh
uv run --locked python -m instagram_pilot.qualify --target APPROVED_HANDLE --baseline data/validation/instagram-live/first.json --output data/validation/instagram-live/second.json
```

The output includes the returned handles and account IDs, pages read, exact
profile count when available, and coverage for each direction. If a terminal
page disagrees with the profile count, the capture records the mismatch and
compares the accounts Instagram returned. An interrupted list is ineligible for
comparison.

## Live release gate

On November 17, 2026, a controlled check reached the end of both lists on one
profile. The followers roster matched the displayed count. The following roster
contained fewer distinct accounts than the displayed count. Each returned
member had a numeric account ID. The discrepancy remains unexplained.

Meta's Instagram API documents follower and following counts. We found no
documented endpoint for the named rosters. Meta requires express permission for
automated collection, including with a signed-in account.
[Instagram account API](https://developers.facebook.com/docs/graph-api/reference/shadow-ig-user)
and [Instagram terms](https://help.instagram.com/581066165581870/).

Before enabling live scheduling, qualify a Meta-authorized source that supplies
named rosters with stable target and observer IDs and permits retaining and
sharing the captures with current case members. Validate the approved personal
connection and credential lifecycle on a self-hosted installation. Establish
independent baselines, complete three actual daily checks with the app tab closed,
and verify that a service restart between checks produces one delivery. Verify
an addition in each direction at unchanged totals and a check with no additions.
