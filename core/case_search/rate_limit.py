"""A shared, nonblocking budget for the Cloudflare AI Search REST API."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.db import connection
from django.utils.http import parse_http_date_safe

from core.models import CloudflareSearchRateLimit

# At 3.5 requests/second plus the burst, Pandect can reserve about 1,074
# requests in five minutes, leaving capacity under Cloudflare's 1,200 limit.
REQUESTS_PER_SECOND = 3.5
# A full cross-case search page makes three requests for each of eight cases.
BURST = 24
REQUEST_INTERVAL = timedelta(seconds=1 / REQUESTS_PER_SECOND)
FALLBACK_DELAY = timedelta(minutes=5)


def retry_deadline(value, now):
    """Read either Retry-After form or fall back to the API rate-limit window."""
    if value:
        value = value.strip()
        try:
            if value.isascii() and value.isdecimal():
                return now + timedelta(seconds=max(1, int(value)))
            if (stamp := parse_http_date_safe(value)) is not None:
                return max(
                    now + timedelta(seconds=1), datetime.fromtimestamp(stamp, UTC)
                )
        except ValueError, OverflowError, OSError:
            pass
    return now + FALLBACK_DELAY


@contextmanager
def budget(account):
    # A delivery holds its index lock in a separate transaction. Budget updates must
    # be committed promptly so other processes can see them during that delivery.
    database = connection.copy()
    try:
        yield AccountBudget(account.lower(), database)
    finally:
        database.close()


class AccountBudget:
    def __init__(self, account, database):
        self.account = account
        self.database = database
        self.table = database.ops.quote_name(CloudflareSearchRateLimit._meta.db_table)

    def reserve(self, requests):
        """Reserve request capacity or return the next eligible time without waiting."""
        if not 1 <= requests <= BURST:
            raise ValueError("Invalid Cloudflare request reservation.")
        # scheduled_at is when existing reservations would drain. Charge the
        # whole reservation only if its new drain time fits inside the burst.
        charge = REQUEST_INTERVAL * requests
        credit = REQUEST_INTERVAL * (BURST - requests)
        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {self.table} AS budget (id, account, scheduled_at)
                VALUES (%s, %s, statement_timestamp() + %s)
                ON CONFLICT (account) DO UPDATE
                SET scheduled_at = GREATEST(budget.scheduled_at, statement_timestamp())
                    + %s
                WHERE (budget.resume_at IS NULL
                    OR budget.resume_at <= statement_timestamp())
                    AND budget.scheduled_at <= statement_timestamp() + %s
                RETURNING scheduled_at
                """,
                [uuid4(), self.account, charge, charge, credit],
            )
            if cursor.fetchone():
                return None
            cursor.execute(
                f"""
                SELECT GREATEST(scheduled_at - %s, resume_at)
                FROM {self.table}
                WHERE account = %s
                """,
                [credit, self.account],
            )
            return cursor.fetchone()[0]

    def cooldown(self):
        """Check Cloudflare backoff even after request capacity is reserved."""
        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT resume_at FROM {self.table}
                WHERE account = %s AND resume_at > statement_timestamp()
                """,
                [self.account],
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def throttle(self, deadline):
        """Share a 429 deadline while preserving any longer active backoff."""
        with self.database.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {self.table} AS budget
                    (id, account, scheduled_at, resume_at)
                VALUES (%s, %s, statement_timestamp(), %s)
                ON CONFLICT (account) DO UPDATE
                SET resume_at = GREATEST(budget.resume_at, EXCLUDED.resume_at)
                RETURNING resume_at
                """,
                [uuid4(), self.account, deadline],
            )
            return cursor.fetchone()[0]
