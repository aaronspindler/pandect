"""Limits for ordinary chat searches against local case indexes."""

from contextlib import contextmanager
from time import monotonic

from django.db import OperationalError, connection, transaction

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

# Three planned reads plus one interrupted delivery. Each ranking can open at
# most 64 candidate identities, including unsuccessful integrity checks.
MAX_LOOKUPS = 4
MAX_CANDIDATES = 64
SQL_SECONDS = 3


def reserve_lookup(turn):
    attempts = turn.plan.get("lookup_attempts", 0)
    if attempts >= MAX_LOOKUPS:
        raise AnalysisError("chat_budget_exhausted")
    # A worker owns a unique delivery token. Save before reading any candidates
    # so recovery from a crash consumes the original allowance.
    turn.plan["lookup_attempts"] = attempts + 1
    updated = ChatTurn.objects.filter(
        pk=turn.pk,
        case_id=turn.case_id,
        delivery_token=turn.delivery_token,
        status=ChatTurn.Status.RUNNING,
    ).update(plan=turn.plan)
    if not updated:
        raise AnalysisError("processing_cancelled")


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
