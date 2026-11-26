"""Bound source verification for historical saved selections."""

from contextlib import contextmanager
from time import monotonic

from django.db import OperationalError, connection, transaction

from core.analysis.errors import AnalysisError

SQL_SECONDS = 3


@contextmanager
def timed_query():
    """Bound a batch of local SELECTs and respect any tighter operator timeout."""
    deadline = monotonic() + SQL_SECONDS

    def within_deadline(execute, sql, params, many, context):
        if monotonic() >= deadline:
            raise AnalysisError("chat_budget_exhausted")
        return execute(sql, params, many, context)

    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'"
            )
            previous = cursor.fetchone()[0]
            bound = int(SQL_SECONDS * 1000)
            changed = previous == 0 or previous > bound
            if changed:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)", [f"{bound}ms"]
                )
            with connection.execute_wrapper(within_deadline):
                yield
            if monotonic() >= deadline:
                raise AnalysisError("chat_budget_exhausted")
            if changed:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [f"{previous}ms"],
                )
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", "") == "57014":
            raise AnalysisError("chat_budget_exhausted") from None
        raise
