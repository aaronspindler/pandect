"""Private, bounded handoff between the web worker and the database ingester."""

import json
import os
import sqlite3
import stat
from collections.abc import Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
BUSY_TIMEOUT = 0.25
HEARTBEAT_GRACE = timedelta(seconds=45)


@contextmanager
def _connect(path, *, create=False):
    path = Path(path)
    if create:
        mode = path.parent.stat(follow_symlinks=False).st_mode
        if not stat.S_ISDIR(mode) or mode & 0o077:
            raise PermissionError("Request activity spool needs a private directory")
        try:
            fd = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise PermissionError("Request activity spool needs a private file")
    with closing(
        sqlite3.connect(
            f"{path.absolute().as_uri()}?mode=rw", uri=True, timeout=BUSY_TIMEOUT
        )
    ) as db:
        yield db


@contextmanager
def open_writer(path):
    with _connect(path, create=True) as db:
        db.execute("PRAGMA synchronous = EXTRA")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_limit = MAX_BYTES // page_size
        if (
            db.execute(f"PRAGMA max_page_count = {page_limit}").fetchone()[0]
            != page_limit
        ):
            raise OSError("Request activity spool exceeds its capacity")
        with db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_request (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT UNIQUE NOT NULL,
                    queued_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_health (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    heartbeat_at TEXT NOT NULL,
                    last_gap_at TEXT
                )
                """
            )
        yield db


def write_batch(db, events, heartbeat_at, last_gap_at):
    with db:
        db.executemany(
            """
            INSERT INTO pending_request (event_id, queued_at, payload)
            VALUES (?, ?, ?) ON CONFLICT (event_id) DO NOTHING
            """,
            (
                (
                    event["event_id"],
                    heartbeat_at,
                    json.dumps(event, separators=(",", ":")),
                )
                for event in events
            ),
        )
        db.execute(
            """
            INSERT INTO capture_health (singleton, heartbeat_at, last_gap_at)
            VALUES (1, ?, ?)
            ON CONFLICT (singleton) DO UPDATE SET
                heartbeat_at = max(capture_health.heartbeat_at, excluded.heartbeat_at),
                last_gap_at = nullif(
                    max(coalesce(capture_health.last_gap_at, ''),
                        coalesce(excluded.last_gap_at, '')), '')
            """,
            (heartbeat_at, last_gap_at),
        )


def read_batch(path: str | Path, limit: int) -> list[dict]:
    """Return pending events without removing them."""
    with _connect(path) as db:
        if db is None:
            return []
        try:
            rows = db.execute(
                "SELECT payload FROM pending_request ORDER BY sequence LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        return [json.loads(payload) for (payload,) in rows]


def acknowledge(path: str | Path, event_ids: Sequence[str]) -> None:
    """Release records only after the corresponding PostgreSQL commit."""
    with _connect(path) as db:
        if db is not None:
            with db:
                db.executemany(
                    "DELETE FROM pending_request WHERE event_id = ?",
                    ((event_id,) for event_id in event_ids),
                )


def parse_health_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except TypeError, ValueError, OverflowError:
        return None


def heartbeat_is_stale(heartbeat, *, now):
    return heartbeat is None or now - heartbeat > HEARTBEAT_GRACE


def read_health(path: str | Path) -> dict | None:
    """Read capture health and decode persisted timestamps as UTC instants."""
    with _connect(path) as db:
        if db is None:
            return None
        try:
            row = db.execute(
                """
                SELECT heartbeat_at, last_gap_at,
                    (SELECT queued_at FROM pending_request ORDER BY sequence LIMIT 1),
                    (SELECT count(*) FROM pending_request)
                FROM capture_health WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        if row is None:
            return None
        return {
            "heartbeat_at": parse_health_timestamp(row[0]),
            "last_gap_at": parse_health_timestamp(row[1]),
            "queued_oldest_at": parse_health_timestamp(row[2]),
            "queued_count": row[3],
        }
