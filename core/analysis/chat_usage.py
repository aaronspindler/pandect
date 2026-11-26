"""Reserve and reconcile bounded AI Gateway usage for each investigation."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db import transaction

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

# Attempts, transferred bytes, input tokens and output tokens per question.
ORDINARY_LIMITS = (12, 256 * 1024, 96 * 1024, 32 * 1024)
FULL_REVIEW_LIMITS = (256, 8 * 1024 * 1024, 3 * 1024 * 1024, 1024 * 1024)


@dataclass(frozen=True)
class Reservation:
    turn_id: UUID
    index: int
    output_limit: int

    @transaction.atomic
    def settle(self, input_tokens, output_tokens):
        """Reconcile reported totals once. Missing counts keep their reservation."""
        if not any(
            type(n) is int and 0 <= n < 2**31 for n in (input_tokens, output_tokens)
        ):
            return
        turn = (
            ChatTurn.objects.select_for_update()
            .only("model_usage")
            .filter(pk=self.turn_id)
            .first()
        )
        if turn is None:
            return
        entry = turn.model_usage[self.index]
        for key, n in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
        ):
            if key not in entry and type(n) is int and 0 <= n < 2**31:
                entry[key] = n
        if entry.get("rates_usd_per_million") and all(
            k in entry for k in ("input_tokens", "output_tokens")
        ):
            input_rate, output_rate = map(Decimal, entry["rates_usd_per_million"])
            cost = (
                entry["input_tokens"] * input_rate
                + entry["output_tokens"] * output_rate
            ) / 1_000_000
            entry["estimated_usd"] = str(cost)
        turn.save(update_fields=["model_usage"])


def reserve(turn, purpose, size, *, output_limit=0, input_bound=None):
    """Serialize reservations across dispatch, crash recovery and transport retries."""
    if turn is None:
        return None
    attempts, transfer_limit, input_limit, total_output = (
        FULL_REVIEW_LIMITS if turn.mode == "full" else ORDINARY_LIMITS
    )
    with transaction.atomic():
        current = (
            ChatTurn.objects.select_for_update(of=("self",))
            .filter(
                pk=turn.pk,
                case_id=turn.case_id,
                chat__case_id=turn.case_id,
                delivery_token=turn.delivery_token,
                status="running",
            )
            .only("model_usage")
            .first()
        )
        if current is None:
            raise AnalysisError("processing_cancelled")
        entries = current.model_usage
        # Reserve conservatively until the gateway reports actual token counts.
        estimated_input = size + 512 if input_bound is None else input_bound
        input_used = sum(row.get("input_tokens", row["input_bound"]) for row in entries)
        output_used = sum(
            row.get("output_tokens", row["output_limit"]) for row in entries
        )
        remaining_bytes = transfer_limit - sum(row["bytes"] for row in entries)
        allowed = min(output_limit, max(0, total_output - output_used))
        if (
            len(entries) >= attempts
            or size > remaining_bytes
            or estimated_input + input_used > input_limit
            or (output_limit and not allowed)
        ):
            raise AnalysisError("chat_budget_exhausted")
        reservation = Reservation(turn.pk, len(entries), allowed)
        entries.append(
            {
                "purpose": purpose,
                "bytes": size,
                "input_bound": estimated_input,
                "output_limit": allowed,
            }
        )
        entry = entries[-1]
        provider, model = (
            turn.execution.get("provider", ""),
            turn.execution.get("model", ""),
        )
        entry.update(provider=provider, model=model)
        rates = settings.PANDECT_AI_PRICES_USD_PER_MILLION.get(f"{provider}/{model}")
        if rates:
            entry["rates_usd_per_million"] = [str(rate) for rate in rates]
        current.save(update_fields=["model_usage"])
        return reservation
