# SPDX-License-Identifier: GPL-3.0-or-later
"""Regulated values (ARERA tariffs, system charges, taxes) with validity dates.

This is the built-in table for household customers in low voltage. It covers
only the values that change yearly or quarterly. Market values that change
every month (PUN, dispatching, capacity market, CDispD) are not here: they are
passed to the calculator as market data.

A lookup outside the covered dates raises RegulatedDataError: the engine never
guesses a tariff.

Pure Python: no Home Assistant imports are allowed in this package.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise


class Component(StrEnum):
    """A regulated value; the unit is part of the name."""

    # Transport, distribution and metering (TD tariff for household customers).
    NETWORK_FIXED_EUR_POD_YEAR = "network_fixed_eur_pod_year"  # sigma1
    NETWORK_POWER_EUR_KW_YEAR = "network_power_eur_kw_year"  # sigma2
    NETWORK_ENERGY_EUR_KWH = "network_energy_eur_kwh"  # sigma3
    UC3_EUR_KWH = "uc3_eur_kwh"
    UC6_EUR_KWH = "uc6_eur_kwh"
    UC6_POWER_EUR_KW_YEAR = "uc6_power_eur_kw_year"
    # General system charges. The fixed quota applies to non-residents only.
    ASOS_EUR_KWH = "asos_eur_kwh"
    ARIM_EUR_KWH = "arim_eur_kwh"
    ASOS_NON_RESIDENT_EUR_POD_YEAR = "asos_non_resident_eur_pod_year"
    # Sale-side regulated component, also billed by free-market suppliers.
    DISP_BT_EUR_POD_YEAR = "disp_bt_eur_pod_year"
    # Regulated service (tutela) sale prices, EUR/kWh as billed on metered kWh.
    TUTELA_PE_MONO_EUR_KWH = "tutela_pe_mono_eur_kwh"
    TUTELA_PE_F1_EUR_KWH = "tutela_pe_f1_eur_kwh"
    TUTELA_PE_F23_EUR_KWH = "tutela_pe_f23_eur_kwh"
    TUTELA_PD_EUR_KWH = "tutela_pd_eur_kwh"
    TUTELA_PPE_EUR_KWH = "tutela_ppe_eur_kwh"
    TUTELA_PCV_EUR_POD_YEAR = "tutela_pcv_eur_pod_year"
    # Taxes.
    EXCISE_EUR_KWH = "excise_eur_kwh"
    VAT_RATE = "vat_rate"


class RegulatedDataError(LookupError):
    """No regulated value is known for the requested component and date."""


@dataclass(frozen=True, slots=True)
class Rate:
    """One value of a component, valid from `valid_from` to `valid_to` inclusive."""

    component: Component
    valid_from: date
    valid_to: date
    value: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class Span:
    """A run of consecutive days sharing the same value."""

    start: date
    end: date
    value: Decimal

    @property
    def days(self) -> int:
        """Number of days in the span, both ends included."""
        return (self.end - self.start).days + 1


_ARERA_SMT = (
    "ARERA, 'Condizioni economiche per i clienti del Servizio di maggior tutela', "
    "E2026-smt.xlsx, sheets gen-mar / apr-giu / lug-set / ott-dic 2026"
)
_TAX = "Testo Unico Accise, household use; VAT 10% household supply"

_QUARTERS_2026: tuple[tuple[date, date], ...] = (
    (date(2026, 1, 1), date(2026, 3, 31)),
    (date(2026, 4, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 9, 30)),
    (date(2026, 10, 1), date(2026, 12, 31)),
)

_C = Component

# One value per quarter of 2026, in order. Source: _ARERA_SMT.
_BY_QUARTER_2026: tuple[tuple[Component, str, str, str, str], ...] = (
    (_C.NETWORK_FIXED_EUR_POD_YEAR, "23.04", "23.04", "23.04", "23.04"),
    (_C.NETWORK_POWER_EUR_KW_YEAR, "23.52", "23.52", "23.52", "23.52"),
    (_C.NETWORK_ENERGY_EUR_KWH, "0.011900", "0.011900", "0.011900", "0.011900"),
    (_C.UC3_EUR_KWH, "0.002760", "0.002760", "0.002760", "0.002760"),
    (_C.UC6_EUR_KWH, "0.000070", "0.000070", "0.000070", "0.000070"),
    (_C.UC6_POWER_EUR_KW_YEAR, "0.1988", "0.1988", "0.1988", "0.1988"),
    (_C.ASOS_EUR_KWH, "0.028657", "0.028657", "0.031515", "0.031515"),
    (_C.ARIM_EUR_KWH, "0.001638", "0.001638", "0.001638", "0.001638"),
    (_C.ASOS_NON_RESIDENT_EUR_POD_YEAR, "88.7520", "88.7520", "95.0916", "95.0916"),
    (_C.DISP_BT_EUR_POD_YEAR, "1.2311", "1.2311", "1.1070", "1.1070"),
    (_C.TUTELA_PE_MONO_EUR_KWH, "0.12464", "0.15086", "0.15384", "0.26898"),
    (_C.TUTELA_PE_F1_EUR_KWH, "0.12431", "0.15112", "0.14448", "0.26633"),
    (_C.TUTELA_PE_F23_EUR_KWH, "0.12480", "0.15074", "0.15881", "0.27035"),
    (_C.TUTELA_PD_EUR_KWH, "0.02317", "0.01752", "0.02556", "0.01773"),
    (_C.TUTELA_PPE_EUR_KWH, "-0.01027", "-0.01027", "-0.01027", "-0.01027"),
    (_C.TUTELA_PCV_EUR_POD_YEAR, "43.50", "43.50", "41.18", "41.18"),
)

# Valid for the whole of 2026. Source: _TAX.
_ANNUAL_2026: tuple[tuple[Component, str], ...] = (
    (_C.EXCISE_EUR_KWH, "0.0227"),
    (_C.VAT_RATE, "0.10"),
)

RATES: tuple[Rate, ...] = (
    *(
        Rate(component, valid_from, valid_to, Decimal(value), _ARERA_SMT)
        for component, *values in _BY_QUARTER_2026
        for (valid_from, valid_to), value in zip(_QUARTERS_2026, values, strict=True)
    ),
    *(
        Rate(component, date(2026, 1, 1), date(2026, 12, 31), Decimal(value), _TAX)
        for component, value in _ANNUAL_2026
    ),
)


def find_rate(component: Component, day: date, rates: tuple[Rate, ...] = RATES) -> Rate:
    """Return the rate in force for `component` on `day`."""
    for item in rates:
        if item.component is component and item.valid_from <= day <= item.valid_to:
            return item
    raise RegulatedDataError(f"no value for {component} on {day.isoformat()}")


def rate(component: Component, day: date, rates: tuple[Rate, ...] = RATES) -> Decimal:
    """Return the value in force for `component` on `day`."""
    return find_rate(component, day, rates).value


def spans(
    component: Component, start: date, end: date, rates: tuple[Rate, ...] = RATES
) -> list[Span]:
    """Split `start`..`end` (inclusive) into runs of days with the same value.

    Tariffs can change in the middle of a billing period, so charges are
    computed span by span.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
    result: list[Span] = []
    day = start
    while day <= end:
        item = find_rate(component, day, rates)
        last = min(item.valid_to, end)
        if result and result[-1].value == item.value:
            result[-1] = Span(result[-1].start, last, item.value)
        else:
            result.append(Span(day, last, item.value))
        day = last + timedelta(days=1)
    return result


def table_problems(rates: tuple[Rate, ...] = RATES) -> list[str]:
    """Return the inconsistencies found in a rate table (empty when sound).

    Checks that no period is reversed and that two periods of the same
    component never overlap. Gaps are allowed: they raise at lookup time.
    """
    problems: list[str] = []
    by_component: dict[Component, list[Rate]] = {}
    for item in rates:
        if item.valid_to < item.valid_from:
            problems.append(f"{item.component}: period ends before it starts")
        by_component.setdefault(item.component, []).append(item)
    for component, items in by_component.items():
        items.sort(key=lambda item: item.valid_from)
        problems.extend(
            f"{component}: periods overlap on {later.valid_from.isoformat()}"
            for earlier, later in pairwise(items)
            if later.valid_from <= earlier.valid_to
        )
    return problems
