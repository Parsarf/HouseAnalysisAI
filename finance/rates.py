"""Static historical mortgage rate index (spec §6.5 fallback).

Annual-average 30-year fixed rates (Freddie Mac PMMS annual averages,
rounded to the basis point) keyed by origination year. The DB-backed
`historical_rate_index` table (owned by a sibling work package) supersedes
this module once it ships; until then this is the offline source of truth.
"""
from decimal import Decimal

ZERO = Decimal("0")

_CONVENTIONAL_30YR: dict[int, Decimal] = {
    1971: Decimal("0.0754"), 1972: Decimal("0.0738"), 1973: Decimal("0.0796"),
    1974: Decimal("0.0919"), 1975: Decimal("0.0905"), 1976: Decimal("0.0887"),
    1977: Decimal("0.0885"), 1978: Decimal("0.0964"), 1979: Decimal("0.1120"),
    1980: Decimal("0.1374"), 1981: Decimal("0.1664"), 1982: Decimal("0.1604"),
    1983: Decimal("0.1324"), 1984: Decimal("0.1388"), 1985: Decimal("0.1243"),
    1986: Decimal("0.1019"), 1987: Decimal("0.1021"), 1988: Decimal("0.1034"),
    1989: Decimal("0.1032"), 1990: Decimal("0.1013"), 1991: Decimal("0.0925"),
    1992: Decimal("0.0839"), 1993: Decimal("0.0731"), 1994: Decimal("0.0838"),
    1995: Decimal("0.0793"), 1996: Decimal("0.0781"), 1997: Decimal("0.0760"),
    1998: Decimal("0.0694"), 1999: Decimal("0.0744"), 2000: Decimal("0.0805"),
    2001: Decimal("0.0697"), 2002: Decimal("0.0654"), 2003: Decimal("0.0583"),
    2004: Decimal("0.0584"), 2005: Decimal("0.0587"), 2006: Decimal("0.0641"),
    2007: Decimal("0.0634"), 2008: Decimal("0.0603"), 2009: Decimal("0.0504"),
    2010: Decimal("0.0469"), 2011: Decimal("0.0445"), 2012: Decimal("0.0366"),
    2013: Decimal("0.0398"), 2014: Decimal("0.0417"), 2015: Decimal("0.0385"),
    2016: Decimal("0.0365"), 2017: Decimal("0.0399"), 2018: Decimal("0.0454"),
    2019: Decimal("0.0394"), 2020: Decimal("0.0311"), 2021: Decimal("0.0296"),
    2022: Decimal("0.0534"), 2023: Decimal("0.0681"), 2024: Decimal("0.0672"),
    2025: Decimal("0.0670"),
}

# Spread over the conventional 30-year rate, by loan type.
_LOAN_TYPE_SPREAD: dict[str, Decimal] = {
    "conventional": ZERO,
    "fha": Decimal("-0.0020"),
    "va": Decimal("-0.0025"),
    "second": Decimal("0.0150"),
    "heloc": Decimal("0.0200"),
    "hard_money": Decimal("0.0400"),
}


def historical_rate(year: int, loan_type: str = "conventional") -> Decimal | None:
    """Annual-average rate for `year` x `loan_type`, as a Decimal fraction.

    Years outside the table clamp to the nearest known year. Unknown loan
    types fall back to the conventional rate. Returns None only for an
    unusable year.
    """
    if not _CONVENTIONAL_30YR:
        return None
    base = _CONVENTIONAL_30YR.get(year)
    if base is None:
        if year < min(_CONVENTIONAL_30YR):
            base = _CONVENTIONAL_30YR[min(_CONVENTIONAL_30YR)]
        elif year > max(_CONVENTIONAL_30YR):
            base = _CONVENTIONAL_30YR[max(_CONVENTIONAL_30YR)]
        else:
            return None
    return base + _LOAN_TYPE_SPREAD.get(loan_type, ZERO)
