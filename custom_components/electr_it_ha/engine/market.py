# SPDX-License-Identifier: GPL-3.0-or-later
"""Market values a contract needs, by name.

The names are what the configuration stores and what data providers return:
`pun_<band>` for each contract band, `dispatching`, `capacity`, `cdispd`.

Pure Python: no Home Assistant imports are allowed in this package.
"""

from collections.abc import Mapping
from decimal import Decimal

from .calculator import CalculationError
from .contract import BAND_KEYS, Contract, DispatchingMode, PriceType
from .estimate import Market

PUN_PREFIX = "pun_"
DISPATCHING = "dispatching"
CAPACITY = "capacity"
CDISPD = "cdispd"

# Divisor that turns a price in the given unit into EUR/kWh.
_UNIT_DIVISORS: dict[str, Decimal] = {
    "€/kWh": Decimal(1),
    "EUR/kWh": Decimal(1),
    "€/MWh": Decimal(1000),
    "EUR/MWh": Decimal(1000),
}


def required_market_values(contract: Contract) -> tuple[str, ...]:
    """Return the names of the market values `contract` cannot be priced without."""
    names: list[str] = []
    if contract.price_type is PriceType.INDEXED:
        names.extend(f"{PUN_PREFIX}{band}" for band in BAND_KEYS[contract.time_bands])
    if contract.dispatching.mode is DispatchingMode.REGULATED:
        names.append(DISPATCHING)
    if contract.dispatching.mode is DispatchingMode.CDISPD:
        names.append(CDISPD)
    if contract.dispatching.capacity_market:
        names.append(CAPACITY)
    return tuple(names)


def build_market(contract: Contract, values: Mapping[str, Decimal]) -> Market:
    """Build the Market for `contract` from values keyed by name, in EUR/kWh.

    Values the contract does not need are ignored; a missing one is an error.
    """
    required = required_market_values(contract)
    missing = [name for name in required if name not in values]
    if missing:
        raise CalculationError(f"missing market values: {', '.join(missing)}")
    pun = {
        name.removeprefix(PUN_PREFIX): values[name]
        for name in required
        if name.startswith(PUN_PREFIX)
    }
    return Market(
        pun=pun or None,
        dispatching=values[DISPATCHING] if DISPATCHING in required else None,
        capacity=values[CAPACITY] if CAPACITY in required else None,
        cdispd=values[CDISPD] if CDISPD in required else None,
    )


def to_eur_per_kwh(value: Decimal, unit: str | None) -> Decimal:
    """Convert a price to EUR/kWh; no unit means it already is."""
    if unit is None:
        return value
    try:
        return value / _UNIT_DIVISORS[unit]
    except KeyError:
        raise CalculationError(f"unsupported price unit '{unit}'") from None
