"""Dedicated PostgreSQL storage for operational request records."""

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from ipaddress import IPv6Address, ip_address
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

SCHEMA_VERSION = 1
EVENTS = sql.Identifier("request_activity", "events")
FINGERPRINT_PATTERN = re.compile(r"v[1-9][0-9]{0,8}:[0-9a-f]{64}")


class StoreUnavailable(Exception):
    """Safe error for a missing or unavailable telemetry store."""


def normalize_ip(value):
    try:
        address = ip_address(value.strip())
    except AttributeError, ValueError:
        raise ValueError("Enter an IPv4 or IPv6 address.") from None
    if isinstance(address, IPv6Address):
        if address.scope_id is not None:
            raise ValueError("Enter an IPv4 or IPv6 address without a scope ID.")
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return str(address)


def normalize_fingerprint(value):
    if not isinstance(value, str):
        raise ValueError("Enter a complete versioned fingerprint.")
    value = value.strip().lower()
    if not FINGERPRINT_PATTERN.fullmatch(value):
        raise ValueError("Enter a complete versioned fingerprint.")
    return value


def _connection_info(role):
    dsn = os.environ.get(f"PANDECT_REQUEST_ACTIVITY_{role}_DSN", "")
    try:
        values = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise StoreUnavailable(
            "The request activity database connection is not configured."
        ) from None
    if not values.get("dbname") or not values.get("user"):
        raise StoreUnavailable(
            "The request activity database connection is not configured."
        )
    if values["dbname"] == os.environ.get("POSTGRES_DB", "pandect"):
        raise StoreUnavailable(
            "Request activity requires a database separate from Pandect."
        )
    return dsn


def _connect(role):
    options = "-c statement_timeout=5000 -c lock_timeout=1000 -c TimeZone=UTC"
    if role == "READER":
        options += " -c default_transaction_read_only=on"
    try:
        return psycopg.connect(
            _connection_info(role),
            autocommit=True,
            connect_timeout=5,
            options=options,
            row_factory=dict_row,
        )
    except psycopg.Error:
        raise StoreUnavailable(
            "The request activity database is unavailable."
        ) from None


def initialize_schema():
    """Apply the initial, independently managed schema before ingest starts."""
    try:
        with _connect("MIGRATION") as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(1645204998)")
            connection.execute("CREATE SCHEMA IF NOT EXISTS request_activity")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS request_activity.schema_revision (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    version integer NOT NULL
                )
                """)
            revision = connection.execute(
                "SELECT version FROM request_activity.schema_revision"
            ).fetchone()
            if revision:
                if revision["version"] != SCHEMA_VERSION:
                    raise StoreUnavailable(
                        "The request activity database has an unsupported schema."
                    )
                return
            connection.execute("""
                CREATE TABLE request_activity.events (
                    event_id uuid PRIMARY KEY,
                    occurred_at timestamptz NOT NULL,
                    method text NOT NULL CHECK (method ~ '^[A-Z]{1,16}$'),
                    status smallint CHECK (status BETWEEN 100 AND 599),
                    route text NOT NULL CHECK (route ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
                    ip inet,
                    fingerprint text
                        CHECK (fingerprint ~ '^v[1-9][0-9]{0,8}:[0-9a-f]{64}$'),
                    public_html boolean NOT NULL
                )
                """)
            connection.execute("""
                CREATE INDEX events_time
                ON request_activity.events (occurred_at DESC, event_id DESC)
                """)
            connection.execute("""
                CREATE INDEX events_ip_time
                ON request_activity.events (ip, occurred_at DESC, event_id DESC)
                WHERE ip IS NOT NULL
                """)
            connection.execute("""
                CREATE INDEX events_fingerprint_time
                ON request_activity.events
                    (fingerprint, occurred_at DESC, event_id DESC)
                WHERE fingerprint IS NOT NULL
                """)
            connection.execute("""
                CREATE INDEX events_public_time
                ON request_activity.events (occurred_at) WHERE public_html
                """)
            connection.execute(
                "INSERT INTO request_activity.schema_revision (version) VALUES (%s)",
                (SCHEMA_VERSION,),
            )
    except psycopg.Error:
        raise StoreUnavailable(
            "The request activity schema could not be initialized."
        ) from None


class Writer:
    """Commit a whole spool batch before the caller acknowledges its IDs."""

    def __init__(self):
        _connection_info("WRITER")
        self.connection = None

    @contextmanager
    def _connected(self):
        try:
            if self.connection is None:
                self.connection = _connect("WRITER")
            yield self.connection
        except psycopg.Error:
            self.close()
            raise StoreUnavailable(
                "The request activity writer is unavailable."
            ) from None

    def verify(self):
        with self._connected() as connection:
            grants = connection.execute("""
                SELECT has_schema_privilege('request_activity', 'USAGE')
                   AND has_table_privilege('request_activity.events', 'INSERT') AS ready
                """).fetchone()
        if not grants["ready"]:
            self.close()
            raise StoreUnavailable("The request activity writer needs insert access.")

    def write(self, events):
        with (
            self._connected() as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            cursor.execute("SET LOCAL synchronous_commit = on")
            cursor.executemany(
                """
                INSERT INTO request_activity.events
                    (event_id, occurred_at, method, status, route, ip,
                     fingerprint, public_html)
                VALUES
                    (%(event_id)s, %(occurred_at)s, %(method)s, %(status)s,
                     %(route)s, %(ip)s, %(fingerprint)s, %(public_html)s)
                ON CONFLICT DO NOTHING
                """,
                events,
            )

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


@dataclass(frozen=True)
class RequestRecord:
    event_id: UUID
    occurred_at: datetime
    method: str
    status: int | None
    route: str
    ip: str | None
    fingerprint: str | None
    public_html: bool


@dataclass(frozen=True)
class LookupPage:
    rows: tuple[RequestRecord, ...]
    first_seen: datetime | None
    last_seen: datetime | None
    next_cursor: tuple[datetime, UUID] | None


@dataclass(frozen=True)
class VersionCounts:
    version: str
    distinct_fingerprints: int
    repeat_fingerprints: int


@dataclass(frozen=True)
class RepeatSummary:
    start_date: date
    end_date: date
    eligible_requests: int
    unknown_fingerprints: int
    versions: tuple[VersionCounts, ...]
    latest_record_at: datetime | None


class Reader:
    def __enter__(self):
        self.connection = _connect("READER")
        return self

    def __exit__(self, *exc):
        self.connection.close()

    def _select(self, query, parameters=()):
        try:
            return self.connection.execute(query, parameters).fetchall()
        except psycopg.Error:
            raise StoreUnavailable("Request activity could not be read.") from None

    def lookup(
        self,
        *,
        ip=None,
        fingerprint=None,
        after=None,
        before=None,
        cursor=None,
        limit=50,
    ):
        if not 1 <= limit <= 100:
            raise ValueError("Choose a page size between 1 and 100.")
        filters = [sql.SQL("true")]
        values = []
        if ip is not None:
            filters.append(sql.SQL("ip = %s"))
            values.append(normalize_ip(ip))
        if fingerprint is not None:
            filters.append(sql.SQL("fingerprint = %s"))
            values.append(normalize_fingerprint(fingerprint))

        first_seen = last_seen = None
        if values:
            where = sql.SQL(" AND ").join(filters)
            extrema = self._select(
                sql.SQL("""
                    SELECT
                        (SELECT occurred_at FROM {events} WHERE {where}
                         ORDER BY occurred_at, event_id LIMIT 1) AS first_seen,
                        (SELECT occurred_at FROM {events} WHERE {where}
                         ORDER BY occurred_at DESC, event_id DESC LIMIT 1) AS last_seen
                    """).format(events=EVENTS, where=where),
                values + values,
            )[0]
            first_seen, last_seen = extrema["first_seen"], extrema["last_seen"]

        if after is not None:
            filters.append(sql.SQL("occurred_at >= %s"))
            values.append(after)
        if before is not None:
            filters.append(sql.SQL("occurred_at < %s"))
            values.append(before)
        if cursor is not None:
            filters.append(sql.SQL("(occurred_at, event_id) < (%s, %s)"))
            values.extend(cursor)

        result = self._select(
            sql.SQL("""
                SELECT event_id, occurred_at, method, status, route, host(ip) AS ip,
                       fingerprint, public_html
                FROM {events} WHERE {where}
                ORDER BY occurred_at DESC, event_id DESC LIMIT %s
                """).format(events=EVENTS, where=sql.SQL(" AND ").join(filters)),
            [*values, limit + 1],
        )
        rows = tuple(RequestRecord(**row) for row in result[:limit])
        next_cursor = (
            (rows[-1].occurred_at, rows[-1].event_id) if len(result) > limit else None
        )
        return LookupPage(rows, first_seen, last_seen, next_cursor)

    def repeat_summary(self, *, end_date=None):
        end_date = end_date or datetime.now(UTC).date()
        start_date = end_date - timedelta(days=29)
        after = datetime.combine(start_date, time.min, UTC)
        before = datetime.combine(end_date, time.max, UTC)
        summary = self._select(
            """
                SELECT split_part(fingerprint, ':', 1) AS version,
                       sum(requests)::bigint AS requests,
                       count(fingerprint) AS distinct_fingerprints,
                       count(fingerprint) FILTER (WHERE repeated) AS repeat_fingerprints
                FROM (
                    SELECT fingerprint, count(*) AS requests,
                           min((occurred_at AT TIME ZONE 'UTC')::date)
                               < max((occurred_at AT TIME ZONE 'UTC')::date) AS repeated
                    FROM request_activity.events
                    WHERE public_html AND occurred_at BETWEEN %s AND %s
                    GROUP BY fingerprint
                ) AS observations
                GROUP BY 1 ORDER BY 1
                """,
            (after, before),
        )
        latest = self._select("""
            SELECT occurred_at FROM request_activity.events
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1
            """)
        return RepeatSummary(
            start_date=start_date,
            end_date=end_date,
            eligible_requests=sum(group["requests"] for group in summary),
            unknown_fingerprints=sum(
                group["requests"] for group in summary if not group["version"]
            ),
            versions=tuple(
                VersionCounts(
                    group["version"],
                    group["distinct_fingerprints"],
                    group["repeat_fingerprints"],
                )
                for group in summary
                if group["version"] is not None
            ),
            latest_record_at=latest[0]["occurred_at"] if latest else None,
        )
