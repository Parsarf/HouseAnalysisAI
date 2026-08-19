"""Shared historical mortgage rates and amortization helpers."""
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
CONVENTIONAL_30YR = {
    1971: Decimal(".0754"), 1972: Decimal(".0738"), 1973: Decimal(".0796"), 1974: Decimal(".0919"),
    1975: Decimal(".0905"), 1976: Decimal(".0887"), 1977: Decimal(".0885"), 1978: Decimal(".0964"),
    1979: Decimal(".1120"), 1980: Decimal(".1374"), 1981: Decimal(".1664"), 1982: Decimal(".1604"),
    1983: Decimal(".1324"), 1984: Decimal(".1388"), 1985: Decimal(".1243"), 1986: Decimal(".1019"),
    1987: Decimal(".1021"), 1988: Decimal(".1034"), 1989: Decimal(".1032"), 1990: Decimal(".1013"),
    1991: Decimal(".0925"), 1992: Decimal(".0839"), 1993: Decimal(".0731"), 1994: Decimal(".0838"),
    1995: Decimal(".0793"), 1996: Decimal(".0781"), 1997: Decimal(".0760"), 1998: Decimal(".0694"),
    1999: Decimal(".0744"), 2000: Decimal(".0805"), 2001: Decimal(".0697"), 2002: Decimal(".0654"),
    2003: Decimal(".0583"), 2004: Decimal(".0584"), 2005: Decimal(".0587"), 2006: Decimal(".0641"),
    2007: Decimal(".0634"), 2008: Decimal(".0603"), 2009: Decimal(".0504"), 2010: Decimal(".0469"),
    2011: Decimal(".0445"), 2012: Decimal(".0366"), 2013: Decimal(".0398"), 2014: Decimal(".0417"),
    2015: Decimal(".0385"), 2016: Decimal(".0365"), 2017: Decimal(".0399"), 2018: Decimal(".0454"),
    2019: Decimal(".0394"), 2020: Decimal(".0311"), 2021: Decimal(".0296"), 2022: Decimal(".0534"),
    2023: Decimal(".0681"), 2024: Decimal(".0672"), 2025: Decimal(".0670"),
}
LOAN_TYPE_SPREAD = {"conventional": ZERO, "fha": Decimal("-.0020"), "va": Decimal("-.0025"),
                    "second": Decimal(".0150"), "heloc": Decimal(".0200"), "hard_money": Decimal(".0400")}


def historical_rate(year: int, loan_type: str = "conventional") -> Decimal | None:
    if not CONVENTIONAL_30YR:
        return None
    base = CONVENTIONAL_30YR.get(year)
    if base is None:
        base = CONVENTIONAL_30YR[min(CONVENTIONAL_30YR)] if year < min(CONVENTIONAL_30YR) else CONVENTIONAL_30YR[max(CONVENTIONAL_30YR)]
    return base + LOAN_TYPE_SPREAD.get(loan_type, ZERO)


def estimate_balance(original: Decimal | None, rate: Decimal | None, term_months: int | None,
                     origination_date: date | None, as_of: date | None,
                     loan_type: str = "conventional") -> Decimal | None:
    if original is None or original <= ZERO or origination_date is None or as_of is None:
        return None
    term = term_months or 360
    months = max(0, (as_of.year - origination_date.year) * 12 + as_of.month - origination_date.month
                 - (1 if as_of.day < origination_date.day else 0))
    if months >= term:
        return ZERO
    annual = rate if rate is not None else historical_rate(origination_date.year, loan_type)
    if annual is None:
        return None
    if annual == ZERO:
        return original * Decimal(term - months) / Decimal(term)
    monthly = annual / Decimal(12)
    growth = (1 + monthly) ** term
    return original * (growth - (1 + monthly) ** months) / (growth - 1)
