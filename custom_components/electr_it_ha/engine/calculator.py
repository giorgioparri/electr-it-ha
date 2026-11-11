# SPDX-License-Identifier: GPL-3.0-or-later
"""Bill calculator: applies a contract and the regulated values to metered kWh.

Rounding follows what real bills do: every component is its own line, each
line is rounded to the cent, and VAT is computed and rounded line by line.

Lines are built this way:
- supplier prices and market values (index, spread, dispatching): one line per
  period, since they change every month;
- regulated per-kWh components: consecutive periods are merged into one line
  for as long as the tariff does not change;
- fixed and power charges: one line per run of days with the same tariff,
  billed in months;
- excise: one line per period.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from .contract import BAND_KEYS, Contract, DispatchingMode, LossMethod, PriceType
from .regulated import RATES, Component, Rate, rate, spans

_CENT = Decimal("0.01")
# Unit prices multiplied by a loss factor are rounded like the bills show them:
# the index to the hundredth of EUR/MWh, as GME publishes it, the regulated
# dispatching components to the millionth of EUR/kWh.
_INDEX_PRICE_STEP = Decimal("0.00001")
_DISPATCHING_PRICE_STEP = Decimal("0.000001")
_ZERO = Decimal(0)

# Standard network loss factor for low-voltage supplies, applied to the kWh
# billed for dispatching and capacity market. Matches the bills used as tests.
STANDARD_LOSS_FACTOR = Decimal("0.10")

# Excise allowance for residents with up to 3 kW (Testo Unico Accise, art. 52,
# comma 3, letter e): the first 150 kWh per month are exempt; kWh consumed
# above the threshold reduce the exempt quantity one for one.
EXCISE_MAX_POWER_KW = Decimal(3)
EXCISE_EXEMPT_KWH_MONTH = Decimal(150)
EXCISE_LOW_POWER_KW = Decimal("1.5")
EXCISE_THRESHOLD_LOW_POWER_KWH_MONTH = Decimal(150)
EXCISE_THRESHOLD_KWH_MONTH = Decimal(220)

_TUTELA_PE: dict[str, Component] = {
    "f0": Component.TUTELA_PE_MONO_EUR_KWH,
    "f1": Component.TUTELA_PE_F1_EUR_KWH,
    "f23": Component.TUTELA_PE_F23_EUR_KWH,
}


class CalculationError(ValueError):
    """The input data is not enough, or not consistent, to compute the bill."""


class Category(StrEnum):
    """Section of the bill a line belongs to."""

    SALE_ENERGY = "sale_energy"
    SALE_FIXED = "sale_fixed"
    NETWORK = "network"
    SYSTEM_CHARGES = "system_charges"
    EXCISE = "excise"


class Unit(StrEnum):
    """Unit of a line quantity."""

    KWH = "kWh"
    MONTH = "month"
    KW_MONTH = "kW*month"


@dataclass(frozen=True, slots=True)
class Supply:
    """The supply point: what does not depend on the supplier."""

    resident: bool
    power_kw: Decimal


@dataclass(frozen=True, slots=True)
class Period:
    """Metered kWh for a run of days, with the market values of those days.

    `kwh` and `pun` are keyed by band, with the keys of the contract time
    bands. A period must not cross a change of a per-kWh tariff: in practice,
    one period per calendar month. Market values are EUR/kWh and are needed
    only when the contract uses them.
    """

    start: date
    end: date
    kwh: Mapping[str, Decimal | int]
    pun: Mapping[str, Decimal] | None = None
    dispatching: Decimal | None = None
    capacity: Decimal | None = None
    cdispd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the bill."""

    code: str
    category: Category
    start: date
    end: date
    quantity: Decimal
    unit: Unit
    unit_price: Decimal
    amount: Decimal
    vat: Decimal


@dataclass(frozen=True, slots=True)
class Bill:
    """The computed bill; `warnings` lists what was left out and why."""

    lines: tuple[Line, ...]
    warnings: tuple[str, ...] = ()

    @property
    def taxable(self) -> Decimal:
        """Sum of the line amounts, before VAT."""
        return sum((line.amount for line in self.lines), _ZERO)

    @property
    def vat(self) -> Decimal:
        """Sum of the VAT of each line."""
        return sum((line.vat for line in self.lines), _ZERO)

    @property
    def total(self) -> Decimal:
        """Amount due, VAT included."""
        return self.taxable + self.vat

    def subtotal(self, category: Category) -> Decimal:
        """Sum of the amounts of the lines in `category`."""
        return sum(
            (line.amount for line in self.lines if line.category is category), _ZERO
        )

    def lines_for(self, code: str) -> tuple[Line, ...]:
        """Return the lines with the given code, in bill order."""
        return tuple(line for line in self.lines if line.code == code)

    def line(self, code: str) -> Line:
        """Return the only line with the given code."""
        found = self.lines_for(code)
        if len(found) != 1:
            raise KeyError(f"expected one '{code}' line, found {len(found)}")
        return found[0]

    def amount(self, code: str) -> Decimal:
        """Sum of the amounts of the lines with the given code."""
        return sum((line.amount for line in self.lines_for(code)), _ZERO)


def months_between(start: date, end: date) -> Decimal:
    """Return how many months `start`..`end` (inclusive) is billed as.

    Whole months are counted from the start day: 1 June to 31 July is 2, and
    so is 6 July to 5 September. Leftover days count as a fraction of the
    month they fall in.
    """
    if end < start:
        raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")

    def shifted(months: int) -> date:
        year, month0 = divmod(start.year * 12 + start.month - 1 + months, 12)
        last_day = calendar.monthrange(year, month0 + 1)[1]
        return date(year, month0 + 1, min(start.day, last_day))

    whole = 0
    while shifted(whole + 1) - timedelta(days=1) <= end:
        whole += 1
    rest_start = shifted(whole)
    rest_days = (end - rest_start).days + 1
    days_in_month = calendar.monthrange(rest_start.year, rest_start.month)[1]
    return whole + Decimal(rest_days) / days_in_month


def excise_taxable_kwh(kwh: Decimal, supply: Supply, months: Decimal) -> Decimal:
    """Return the kWh subject to excise out of `kwh` consumed in `months`.

    Not verified against a bill yet: the monthly quantities of the law are
    scaled by the length of the period, which may not be how every supplier
    splits a period that does not match calendar months.
    """
    if not supply.resident or supply.power_kw > EXCISE_MAX_POWER_KW:
        return kwh
    threshold_month = (
        EXCISE_THRESHOLD_LOW_POWER_KWH_MONTH
        if supply.power_kw <= EXCISE_LOW_POWER_KW
        else EXCISE_THRESHOLD_KWH_MONTH
    )
    over_threshold = max(_ZERO, kwh - threshold_month * months)
    exempt = max(_ZERO, EXCISE_EXEMPT_KWH_MONTH * months - over_threshold)
    return max(_ZERO, kwh - exempt)


def _cents(value: Decimal) -> Decimal:
    """Round to the cent, half up."""
    return value.quantize(_CENT, ROUND_HALF_UP)


def _round_price(value: Decimal, step: Decimal) -> Decimal:
    """Round a unit price to `step`, half up."""
    return value.quantize(step, ROUND_HALF_UP)


def _loss_kwh(kwh: Decimal, factor: Decimal) -> Decimal:
    """Return the loss kWh for one band, rounded to the whole kWh as on bills."""
    return (kwh * factor).quantize(Decimal(1), ROUND_HALF_UP)


def _check_inputs(
    contract: Contract, supply: Supply, periods: Sequence[Period]
) -> None:
    """Raise CalculationError if the bill cannot be computed from the inputs."""
    if supply.power_kw <= 0:
        raise CalculationError("supply.power_kw must be greater than zero")
    if not periods:
        raise CalculationError("at least one period is required")
    bands = set(BAND_KEYS[contract.time_bands])
    mode = contract.dispatching.mode
    for position, period in enumerate(periods):
        name = f"period {period.start.isoformat()}"
        if period.end < period.start:
            raise CalculationError(f"{name}: ends before it starts")
        if position and period.start != periods[position - 1].end + timedelta(days=1):
            raise CalculationError(f"{name}: must start the day after the previous one")
        if set(period.kwh) != bands:
            raise CalculationError(f"{name}: kwh must have exactly {sorted(bands)}")
        if any(value < 0 for value in period.kwh.values()):
            raise CalculationError(f"{name}: kwh must not be negative")
        if contract.price_type is PriceType.INDEXED and (
            period.pun is None or set(period.pun) != bands
        ):
            raise CalculationError(f"{name}: pun must have exactly {sorted(bands)}")
        if mode is DispatchingMode.CDISPD and period.cdispd is None:
            raise CalculationError(f"{name}: cdispd is required")
        if mode is DispatchingMode.REGULATED and period.dispatching is None:
            raise CalculationError(f"{name}: dispatching is required")
        if contract.dispatching.capacity_market and period.capacity is None:
            raise CalculationError(f"{name}: capacity is required")


class _Builder:
    """Collects the lines of one bill."""

    def __init__(
        self,
        contract: Contract,
        supply: Supply,
        periods: Sequence[Period],
        rates: tuple[Rate, ...],
    ) -> None:
        self.contract = contract
        self.supply = supply
        self.periods = periods
        self.rates = rates
        self.bands = BAND_KEYS[contract.time_bands]
        self.start = periods[0].start
        self.end = periods[-1].end
        self.lines: list[Line] = []
        self.warnings: list[str] = []

    def add(
        self,
        code: str,
        category: Category,
        start: date,
        end: date,
        quantity: Decimal | int,
        unit: Unit,
        unit_price: Decimal,
    ) -> None:
        """Append a line, rounding amount and VAT to the cent."""
        quantity = Decimal(quantity)
        amount = _cents(quantity * unit_price)
        vat = _cents(amount * rate(Component.VAT_RATE, start, self.rates))
        self.lines.append(
            Line(code, category, start, end, quantity, unit, unit_price, amount, vat)
        )

    def total_kwh(self, period: Period) -> Decimal:
        """Return the metered kWh of a period, all bands."""
        return sum((Decimal(period.kwh[band]) for band in self.bands), _ZERO)

    def period_rate(self, component: Component, period: Period) -> Decimal:
        """Return the tariff of a period, which must not change within it."""
        value = rate(component, period.start, self.rates)
        if rate(component, period.end, self.rates) != value:
            raise CalculationError(
                f"period {period.start.isoformat()}: {component} changes within "
                "the period; split the period at the change"
            )
        return value

    def per_kwh(self, code: str, category: Category, component: Component) -> None:
        """Bill a regulated per-kWh component, merging periods with one tariff."""
        run: tuple[date, date, Decimal, Decimal] | None = None
        for period in self.periods:
            value = self.period_rate(component, period)
            kwh = self.total_kwh(period)
            if run is not None and run[3] == value:
                run = (run[0], period.end, run[2] + kwh, value)
                continue
            if run is not None:
                self.add(code, category, run[0], run[1], run[2], Unit.KWH, run[3])
            run = (period.start, period.end, kwh, value)
        if run is not None:
            self.add(code, category, run[0], run[1], run[2], Unit.KWH, run[3])

    def per_month(
        self,
        code: str,
        category: Category,
        component: Component,
        power_kw: Decimal | None = None,
    ) -> None:
        """Bill a regulated yearly charge by the month, per POD or per kW."""
        for span in spans(component, self.start, self.end, self.rates):
            months = months_between(span.start, span.end)
            if power_kw is None:
                quantity, unit = months, Unit.MONTH
            else:
                quantity, unit = months * power_kw, Unit.KW_MONTH
            self.add(
                code, category, span.start, span.end, quantity, unit, span.value / 12
            )

    def sale_energy(self, period: Period) -> None:
        """Add the energy lines of one period, by price type."""
        contract = self.contract
        sale = Category.SALE_ENERGY
        start, end = period.start, period.end
        prices = contract.energy_price or {}
        if contract.price_type is PriceType.INDEXED:
            assert contract.index is not None
            assert period.pun is not None
            index = contract.index
            if index.loss_method is LossMethod.PRICE:
                for band in self.bands:
                    price = _round_price(
                        period.pun[band] * index.coefficient, _INDEX_PRICE_STEP
                    )
                    self.add(
                        f"index_{band}",
                        sale,
                        start,
                        end,
                        period.kwh[band],
                        Unit.KWH,
                        price,
                    )
            else:
                loss_factor = index.coefficient - 1
                for band in self.bands:
                    pun = period.pun[band]
                    self.add(
                        f"index_{band}",
                        sale,
                        start,
                        end,
                        period.kwh[band],
                        Unit.KWH,
                        pun,
                    )
                for band in self.bands:
                    loss = _loss_kwh(Decimal(period.kwh[band]), loss_factor)
                    if loss:
                        pun = period.pun[band]
                        self.add(
                            f"index_loss_{band}", sale, start, end, loss, Unit.KWH, pun
                        )
            for band in self.bands:
                spread = prices.get(band, _ZERO)
                if spread:
                    code = f"spread_{band}"
                    self.add(code, sale, start, end, period.kwh[band], Unit.KWH, spread)
        elif contract.price_type is PriceType.FIXED:
            for band in self.bands:
                code = f"energy_{band}"
                self.add(
                    code, sale, start, end, period.kwh[band], Unit.KWH, prices[band]
                )
        else:
            for band in self.bands:
                price = self.period_rate(_TUTELA_PE[band], period)
                self.add(
                    f"pe_{band}", sale, start, end, period.kwh[band], Unit.KWH, price
                )
            total = self.total_kwh(period)
            for code, component in (
                ("pd", Component.TUTELA_PD_EUR_KWH),
                ("ppe", Component.TUTELA_PPE_EUR_KWH),
            ):
                price = self.period_rate(component, period)
                self.add(code, sale, start, end, total, Unit.KWH, price)

    def sale_dispatching(self, period: Period) -> None:
        """Add dispatching, capacity market and supplier per-kWh fees."""
        contract = self.contract
        sale = Category.SALE_ENERGY
        start, end = period.start, period.end
        total = self.total_kwh(period)
        in_price = (
            contract.index is not None
            and contract.index.loss_method is LossMethod.PRICE
        )
        if in_price:
            quantity = total
        else:
            quantity = total + sum(
                (
                    _loss_kwh(Decimal(period.kwh[band]), STANDARD_LOSS_FACTOR)
                    for band in self.bands
                ),
                _ZERO,
            )

        def with_losses(price: Decimal) -> Decimal:
            """Return the price billed per metered kWh under the loss method."""
            if not in_price:
                return price
            return _round_price(
                price * (1 + STANDARD_LOSS_FACTOR), _DISPATCHING_PRICE_STEP
            )

        mode = contract.dispatching.mode
        if mode is DispatchingMode.CDISPD:
            assert period.cdispd is not None
            self.add("cdispd", sale, start, end, total, Unit.KWH, period.cdispd)
        elif mode is DispatchingMode.REGULATED:
            assert period.dispatching is not None
            price = with_losses(period.dispatching)
            self.add("dispatching", sale, start, end, quantity, Unit.KWH, price)
        elif mode is DispatchingMode.FIXED:
            assert contract.dispatching.fixed_value is not None
            price = with_losses(contract.dispatching.fixed_value)
            self.add("dispatching", sale, start, end, quantity, Unit.KWH, price)
        if contract.dispatching.capacity_market:
            assert period.capacity is not None
            price = with_losses(period.capacity)
            self.add("capacity", sale, start, end, quantity, Unit.KWH, price)
        if contract.energy_fee_eur_kwh:
            price = contract.energy_fee_eur_kwh
            self.add("energy_fee", sale, start, end, total, Unit.KWH, price)
        green = contract.green_energy
        if green is not None and not green.optional and green.eur_kwh:
            self.add("green_energy", sale, start, end, total, Unit.KWH, green.eur_kwh)

    def sale_fixed(self) -> None:
        """Add the fixed sale charges over the whole bill."""
        contract = self.contract
        fixed = Category.SALE_FIXED
        months = months_between(self.start, self.end)
        if contract.fixed_fee_eur_year:
            price = contract.fixed_fee_eur_year / 12
            self.add(
                "fixed_fee", fixed, self.start, self.end, months, Unit.MONTH, price
            )
        green = contract.green_energy
        if green is not None and not green.optional and green.eur_year:
            price = green.eur_year / 12
            self.add(
                "green_fixed", fixed, self.start, self.end, months, Unit.MONTH, price
            )
        regulated = contract.price_type is PriceType.REGULATED
        if regulated:
            self.per_month("pcv", fixed, Component.TUTELA_PCV_EUR_POD_YEAR)
        if regulated or contract.disp_bt:
            self.per_month("disp_bt", fixed, Component.DISP_BT_EUR_POD_YEAR)

    def network(self) -> None:
        """Add transport, distribution and metering."""
        net = Category.NETWORK
        power = self.supply.power_kw
        self.per_kwh("network_energy", net, Component.NETWORK_ENERGY_EUR_KWH)
        self.per_kwh("uc3", net, Component.UC3_EUR_KWH)
        self.per_kwh("uc6", net, Component.UC6_EUR_KWH)
        self.per_month("network_fixed", net, Component.NETWORK_FIXED_EUR_POD_YEAR)
        self.per_month("network_power", net, Component.NETWORK_POWER_EUR_KW_YEAR, power)
        self.per_month("uc6_power", net, Component.UC6_POWER_EUR_KW_YEAR, power)

    def system_charges(self) -> None:
        """Add the general system charges."""
        charges = Category.SYSTEM_CHARGES
        self.per_kwh("asos", charges, Component.ASOS_EUR_KWH)
        self.per_kwh("arim", charges, Component.ARIM_EUR_KWH)
        if not self.supply.resident:
            component = Component.ASOS_NON_RESIDENT_EUR_POD_YEAR
            self.per_month("asos_fixed", charges, component)

    def excise(self) -> None:
        """Add excise, one line per period, on metered kWh only."""
        for period in self.periods:
            months = months_between(period.start, period.end)
            taxable = excise_taxable_kwh(self.total_kwh(period), self.supply, months)
            price = self.period_rate(Component.EXCISE_EUR_KWH, period)
            self.add(
                "excise",
                Category.EXCISE,
                period.start,
                period.end,
                taxable,
                Unit.KWH,
                price,
            )

    def left_out(self) -> None:
        """Record what the contract has that this version does not bill."""
        if self.contract.discounts:
            names = ", ".join(discount.name for discount in self.contract.discounts)
            self.warnings.append(f"discounts are not applied: {names}")
        green = self.contract.green_energy
        if green is not None and green.optional:
            self.warnings.append("optional green energy is not applied")


def compute_bill(
    contract: Contract,
    supply: Supply,
    periods: Sequence[Period],
    rates: tuple[Rate, ...] = RATES,
) -> Bill:
    """Compute the bill for consecutive `periods` of metered consumption.

    Raises CalculationError when the inputs are incomplete or inconsistent,
    and RegulatedDataError when a regulated value is not known for the dates.
    """
    _check_inputs(contract, supply, periods)
    builder = _Builder(contract, supply, periods, rates)
    for period in periods:
        builder.sale_energy(period)
        builder.sale_dispatching(period)
    builder.sale_fixed()
    builder.network()
    builder.system_charges()
    builder.excise()
    builder.left_out()
    return Bill(tuple(builder.lines), tuple(builder.warnings))
