# SPDX-License-Identifier: GPL-3.0-or-later
"""Running estimates between two bills.

Three views of the same contract, built on the bill calculator:
- `unit_price`: what the next kWh costs right now, VAT included;
- `fixed_cost_per_day`: what the supply costs per day with no consumption;
- `month_to_date`: the bill of the current month so far.

They are estimates while the month is open: market values (PUN above all)
are final only when the month ends, and a real bill rounds line by line.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .calculator import (
    STANDARD_LOSS_FACTOR,
    Bill,
    CalculationError,
    Period,
    Supply,
    compute_bill,
    excise_taxable_kwh,
)
from .contract import BAND_KEYS, Contract, DispatchingMode, PriceType
from .regulated import RATES, Component, Rate, rate

_ZERO = Decimal(0)
_ONE = Decimal(1)

_TUTELA_PE: dict[str, Component] = {
    "f0": Component.TUTELA_PE_MONO_EUR_KWH,
    "f1": Component.TUTELA_PE_F1_EUR_KWH,
    "f23": Component.TUTELA_PE_F23_EUR_KWH,
}


@dataclass(frozen=True, slots=True)
class Market:
    """Market values in EUR/kWh, as known or estimated for the current month.

    `pun` is keyed by the contract band keys. Each value is needed only when
    the contract uses it.
    """

    pun: Mapping[str, Decimal] | None = None
    dispatching: Decimal | None = None
    capacity: Decimal | None = None
    cdispd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class UnitPrice:
    """Cost of one more kWh, in EUR/kWh, split by section of the bill."""

    sale: Decimal
    network: Decimal
    system_charges: Decimal
    excise: Decimal
    vat: Decimal

    @property
    def taxable(self) -> Decimal:
        """Price before VAT."""
        return self.sale + self.network + self.system_charges + self.excise

    @property
    def total(self) -> Decimal:
        """Price with VAT."""
        return self.taxable + self.vat


def _required(value: Decimal | None, name: str) -> Decimal:
    """Return a market value, or raise if the contract needs it and it is missing."""
    if value is None:
        raise CalculationError(f"market value '{name}' is required by this contract")
    return value


def _sale_price(
    contract: Contract, market: Market, day: date, band: str, rates: tuple[Rate, ...]
) -> Decimal:
    """Return the sale part of the unit price: energy, dispatching, fees."""
    prices = contract.energy_price or {}
    if contract.price_type is PriceType.INDEXED:
        assert contract.index is not None
        if market.pun is None or band not in market.pun:
            raise CalculationError(f"market value 'pun' is required for band '{band}'")
        price = market.pun[band] * contract.index.coefficient + prices.get(band, _ZERO)
    elif contract.price_type is PriceType.FIXED:
        price = prices[band]
    else:
        price = (
            rate(_TUTELA_PE[band], day, rates)
            + rate(Component.TUTELA_PD_EUR_KWH, day, rates)
            + rate(Component.TUTELA_PPE_EUR_KWH, day, rates)
        )
    with_losses = _ONE + STANDARD_LOSS_FACTOR
    mode = contract.dispatching.mode
    if mode is DispatchingMode.CDISPD:
        price += _required(market.cdispd, "cdispd")
    elif mode is DispatchingMode.REGULATED:
        price += _required(market.dispatching, "dispatching") * with_losses
    elif mode is DispatchingMode.FIXED:
        assert contract.dispatching.fixed_value is not None
        price += contract.dispatching.fixed_value * with_losses
    if contract.dispatching.capacity_market:
        price += _required(market.capacity, "capacity") * with_losses
    price += contract.energy_fee_eur_kwh or _ZERO
    green = contract.green_energy
    if green is not None and not green.optional:
        price += green.eur_kwh or _ZERO
    return price


def unit_price(
    contract: Contract,
    supply: Supply,
    market: Market,
    day: date,
    band: str,
    month_kwh: Decimal = _ZERO,
    rates: tuple[Rate, ...] = RATES,
) -> UnitPrice:
    """Return what one more kWh costs on `day` in the contract band `band`.

    `month_kwh` is the consumption of the calendar month so far: with the
    excise allowance the next kWh can be exempt, taxed, or taxed twice over
    (when it also takes one kWh out of the allowance).
    """
    if band not in BAND_KEYS[contract.time_bands]:
        raise CalculationError(f"band '{band}' is not in the contract time bands")
    sale = _sale_price(contract, market, day, band, rates)
    network = (
        rate(Component.NETWORK_ENERGY_EUR_KWH, day, rates)
        + rate(Component.UC3_EUR_KWH, day, rates)
        + rate(Component.UC6_EUR_KWH, day, rates)
    )
    system_charges = rate(Component.ASOS_EUR_KWH, day, rates) + rate(
        Component.ARIM_EUR_KWH, day, rates
    )
    newly_taxable = excise_taxable_kwh(
        month_kwh + 1, supply, _ONE
    ) - excise_taxable_kwh(month_kwh, supply, _ONE)
    excise = newly_taxable * rate(Component.EXCISE_EUR_KWH, day, rates)
    vat = (sale + network + system_charges + excise) * rate(
        Component.VAT_RATE, day, rates
    )
    return UnitPrice(sale, network, system_charges, excise, vat)


def fixed_cost_per_day(
    contract: Contract,
    supply: Supply,
    day: date,
    rates: tuple[Rate, ...] = RATES,
) -> Decimal:
    """Return the cost of `day` with no consumption, in EUR, VAT included.

    Fixed and power charges are billed by the month, so a day costs one
    month's worth divided by the days of that month.
    """
    per_year = contract.fixed_fee_eur_year or _ZERO
    green = contract.green_energy
    if green is not None and not green.optional:
        per_year += green.eur_year or _ZERO
    regulated = contract.price_type is PriceType.REGULATED
    if regulated:
        per_year += rate(Component.TUTELA_PCV_EUR_POD_YEAR, day, rates)
    if regulated or contract.disp_bt:
        per_year += rate(Component.DISP_BT_EUR_POD_YEAR, day, rates)
    per_year += rate(Component.NETWORK_FIXED_EUR_POD_YEAR, day, rates)
    per_year += supply.power_kw * (
        rate(Component.NETWORK_POWER_EUR_KW_YEAR, day, rates)
        + rate(Component.UC6_POWER_EUR_KW_YEAR, day, rates)
    )
    if not supply.resident:
        per_year += rate(Component.ASOS_NON_RESIDENT_EUR_POD_YEAR, day, rates)
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    with_vat = _ONE + rate(Component.VAT_RATE, day, rates)
    return per_year / 12 / days_in_month * with_vat


def month_to_date(
    contract: Contract,
    supply: Supply,
    market: Market,
    day: date,
    kwh: Mapping[str, Decimal | int],
    rates: tuple[Rate, ...] = RATES,
) -> Bill:
    """Return the bill from the first of the month to `day`, both included.

    `kwh` is the consumption of the month so far, keyed by contract band.
    """
    period = Period(
        start=day.replace(day=1),
        end=day,
        kwh=kwh,
        pun=market.pun,
        dispatching=market.dispatching,
        capacity=market.capacity,
        cdispd=market.cdispd,
    )
    return compute_bill(contract, supply, [period], rates)
