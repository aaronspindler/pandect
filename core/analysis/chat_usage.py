"""Cumulative ordinary-chat allowances reserved at evidence-bearing dispatch."""

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

# Three answer passes can each use query embedding, passage reranking and generation.
# Count the local prompt-rendering transfer separately even though it does not generate.
MAX_ATTEMPTS = 12
MAX_TRANSFER_BYTES = 256 * 1024
MAX_INPUT = 96 * 1024
MAX_OUTPUT = 32 * 1024


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
        if "input_tokens" in entry or "output_tokens" in entry:
            return
        for key, n in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
        ):
            if type(n) is int and 0 <= n < 2**31:
                entry[key] = n
        turn.save(update_fields=["model_usage"])


def reserve(turn, purpose, size, *, output_limit=0, input_bound=None):
    """Serialize reservations across dispatch, crash recovery and transport retries."""
    if turn is None or turn.mode == "full":
        return None
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
        # Serialized UTF-8 bytes approximate input conservatively on routes without
        # a prompt tokenizer. Local Ollama supplies its rendered byte-level bound.
        estimated_input = size + 512 if input_bound is None else input_bound
        input_used = sum(row.get("input_tokens", row["input_bound"]) for row in entries)
        output_used = sum(
            row.get("output_tokens", row["output_limit"]) for row in entries
        )
        allowed = min(output_limit, max(0, MAX_OUTPUT - output_used))
        if (
            len(entries) >= MAX_ATTEMPTS
            or size + sum(row["bytes"] for row in entries) > MAX_TRANSFER_BYTES
            or estimated_input + input_used > MAX_INPUT
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
        current.save(update_fields=["model_usage"])
        return reservation
