"""Optional operator prices in USD per million tokens for local cost estimates."""

import json
import os
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ImproperlyConfigured


def model_prices():
    try:
        raw = json.loads(
            os.environ.get("PANDECT_AI_PRICES_USD_PER_MILLION") or "{}",
            parse_float=Decimal,
        )
        if not isinstance(raw, dict):
            raise ValueError
        prices = {}
        for route, pair in raw.items():
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError
            values = tuple(Decimal(str(value)) for value in pair)
            if not all(
                value.is_finite() and 0 <= value <= 1_000_000 for value in values
            ):
                raise ValueError
            prices[route] = values
        return prices
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "Set PANDECT_AI_PRICES_USD_PER_MILLION to a JSON object mapping "
            "provider/model to two nonnegative USD rates for input and output."
        ) from exc
