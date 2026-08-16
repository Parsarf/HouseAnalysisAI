"""Static transfer-tax rate table (spec §7.5 acquisition costs).

Rates are fractions of the sale price, keyed by
`AcquisitionCosts.transfer_tax_lookup_key` ("STATE" or "STATE:county",
case-insensitive). The DB-backed `transfer_tax_rates` table supersedes this
module once it ships; unknown keys conservatively resolve to 0.
"""
from decimal import Decimal

ZERO = Decimal("0")

TRANSFER_TAX_RATES: dict[str, Decimal] = {
    "CA": Decimal("0.0011"),   # $1.10 per $1,000 statewide documentary tax
    "FL": Decimal("0.0070"),   # doc stamps on deed
    "NY": Decimal("0.0040"),
    "TX": ZERO,
    "WA": Decimal("0.0128"),
}


def transfer_tax_rate(lookup_key: str | None) -> Decimal:
    if not lookup_key:
        return ZERO
    key = lookup_key.strip().upper()
    if key in TRANSFER_TAX_RATES:
        return TRANSFER_TAX_RATES[key]
    state = key.split(":", 1)[0]
    return TRANSFER_TAX_RATES.get(state, ZERO)
