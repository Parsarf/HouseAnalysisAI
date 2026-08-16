from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(value: Decimal | int | str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
