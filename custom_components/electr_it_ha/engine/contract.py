# SPDX-License-Identifier: GPL-3.0-or-later
"""Supply contract model and JSON validation.

The contract holds only what the supplier decides. Everything regulated (PUN,
dispatching values, network tariffs, system charges, excise, VAT) comes from
providers and is not part of this model.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

SCHEMA_VERSION = 1


class PriceType(StrEnum):
    """How the energy price is determined."""

    INDEXED = "indexed"
    FIXED = "fixed"
    REGULATED = "regulated"


class TimeBands(StrEnum):
    """Time-of-use structure of the contract."""

    MONO = "mono"
    F1_F23 = "f1_f23"
    F1_F2_F3 = "f1_f2_f3"


class IndexName(StrEnum):
    """Wholesale index the price follows."""

    PUN_INDEX_GME = "pun_index_gme"


class IndexPeriod(StrEnum):
    """Averaging period of the index."""

    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"


class LossMethod(StrEnum):
    """How the supplier bills the network losses of the index coefficient."""

    ENERGY = "energy"  # extra kWh, rounded to the kWh in each band, at the index
    PRICE = "price"  # index price multiplied by the coefficient


class DispatchingMode(StrEnum):
    """How dispatching costs are charged."""

    CDISPD = "cdispd"  # monthly CDispD value passed through
    REGULATED = "regulated"  # regulated dispatching components, itemized
    FIXED = "fixed"  # fixed EUR/kWh set by the supplier
    INCLUDED = "included"  # already inside the energy price
    TUTELA = "tutela"  # regulated service conditions


class DiscountKind(StrEnum):
    """Unit a discount value is expressed in."""

    PER_KWH = "per_kwh"
    PER_YEAR = "per_year"
    PERCENT = "percent"
    ONE_OFF = "one_off"


# Price keys expected in "energy_price" for each time band structure.
BAND_KEYS: dict[TimeBands, tuple[str, ...]] = {
    TimeBands.MONO: ("f0",),
    TimeBands.F1_F23: ("f1", "f23"),
    TimeBands.F1_F2_F3: ("f1", "f2", "f3"),
}

_ALL_BAND_KEYS = ("f0", "f1", "f2", "f3", "f23")
_CONTRACT_KEYS = (
    "schema_version",
    "supplier",
    "offer_name",
    "offer_code",
    "price_type",
    "time_bands",
    "index",
    "energy_price",
    "dispatching",
    "disp_bt",
    "fixed_fee_eur_year",
    "energy_fee_eur_kwh",
    "green_energy",
    "discounts",
    "notes",
)


class ContractError(ValueError):
    """The contract data is not valid; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        """Store the individual error messages."""
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class Index:
    """Wholesale index reference; `coefficient` carries the loss factor."""

    name: IndexName
    period: IndexPeriod
    coefficient: Decimal
    loss_method: LossMethod = LossMethod.ENERGY


@dataclass(frozen=True, slots=True)
class Dispatching:
    """Dispatching terms; `capacity_market` is true when billed on top."""

    mode: DispatchingMode
    fixed_value: Decimal | None
    capacity_market: bool


@dataclass(frozen=True, slots=True)
class GreenEnergy:
    """Green energy option surcharge."""

    eur_kwh: Decimal | None
    eur_year: Decimal | None
    optional: bool


@dataclass(frozen=True, slots=True)
class Discount:
    """A discount granted by the supplier."""

    name: str
    kind: DiscountKind
    value: Decimal
    condition: str | None
    valid_months: int | None


@dataclass(frozen=True, slots=True)
class Contract:
    """A validated supply contract."""

    supplier: str
    price_type: PriceType
    time_bands: TimeBands
    dispatching: Dispatching
    offer_name: str | None = None
    offer_code: str | None = None
    index: Index | None = None
    # EUR/kWh, losses included. For indexed contracts this is the spread.
    energy_price: Mapping[str, Decimal] | None = None
    disp_bt: bool | None = None
    fixed_fee_eur_year: Decimal | None = None
    energy_fee_eur_kwh: Decimal | None = None
    green_energy: GreenEnergy | None = None
    discounts: tuple[Discount, ...] = ()
    notes: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Validate `data` and build a contract, or raise ContractError."""
        errors: list[str] = []
        contract = _parse_contract(data, errors)
        if errors or contract is None:
            raise ContractError(errors)
        return contract

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Parse a JSON document and build a contract, or raise ContractError."""
        try:
            data = json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError as err:
            raise ContractError([f"invalid JSON: {err}"]) from err
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict, in schema order."""
        return {
            "schema_version": self.schema_version,
            "supplier": self.supplier,
            "offer_name": self.offer_name,
            "offer_code": self.offer_code,
            "price_type": self.price_type.value,
            "time_bands": self.time_bands.value,
            "index": None
            if self.index is None
            else {
                "name": self.index.name.value,
                "period": self.index.period.value,
                "coefficient": _number(self.index.coefficient),
                "loss_method": self.index.loss_method.value,
            },
            "energy_price": None
            if self.energy_price is None
            else {key: _number(value) for key, value in self.energy_price.items()},
            "dispatching": {
                "mode": self.dispatching.mode.value,
                "fixed_value": _number(self.dispatching.fixed_value),
                "capacity_market": self.dispatching.capacity_market,
            },
            "disp_bt": self.disp_bt,
            "fixed_fee_eur_year": _number(self.fixed_fee_eur_year),
            "energy_fee_eur_kwh": _number(self.energy_fee_eur_kwh),
            "green_energy": None
            if self.green_energy is None
            else {
                "eur_kwh": _number(self.green_energy.eur_kwh),
                "eur_year": _number(self.green_energy.eur_year),
                "optional": self.green_energy.optional,
            },
            "discounts": [
                {
                    "name": discount.name,
                    "kind": discount.kind.value,
                    "value": _number(discount.value),
                    "condition": discount.condition,
                    "valid_months": discount.valid_months,
                }
                for discount in self.discounts
            ],
            "notes": list(self.notes),
        }


def _number(value: Decimal | None) -> float | None:
    """Convert a Decimal to a JSON number."""
    return None if value is None else float(value)


def _mapping(
    value: Any, path: str, allowed: tuple[str, ...], errors: list[str]
) -> dict[str, Any] | None:
    """Check that `value` is an object with only known keys."""
    if not isinstance(value, dict):
        errors.append(f"{path or 'contract'}: must be a JSON object")
        return None
    prefix = f"{path}." if path else ""
    errors.extend(
        f"{prefix}{key}: unknown field" for key in value if key not in allowed
    )
    return value


def _string(
    value: Any, path: str, errors: list[str], *, nullable: bool = False
) -> str | None:
    """Read a non-empty string."""
    if value is None:
        if not nullable:
            errors.append(f"{path}: required")
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}: must be a non-empty string")
        return None
    return value.strip()


def _bool(
    value: Any, path: str, errors: list[str], *, nullable: bool = False
) -> bool | None:
    """Read a boolean."""
    if value is None:
        if not nullable:
            errors.append(f"{path}: required")
        return None
    if not isinstance(value, bool):
        errors.append(f"{path}: must be true or false")
        return None
    return value


def _decimal(
    value: Any,
    path: str,
    errors: list[str],
    *,
    nullable: bool = False,
    non_negative: bool = False,
) -> Decimal | None:
    """Read a finite number as Decimal; floats go through their repr."""
    if value is None:
        if not nullable:
            errors.append(f"{path}: required")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        errors.append(f"{path}: must be a number")
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        errors.append(f"{path}: must be a finite number")
        return None
    if non_negative and number < 0:
        errors.append(f"{path}: must not be negative")
        return None
    return number


def _enum[E: StrEnum](
    enum_cls: type[E], value: Any, path: str, errors: list[str]
) -> E | None:
    """Read a required enumerated string."""
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    errors.append(f"{path}: must be one of {', '.join(enum_cls)}")
    return None


def _parse_index(value: Any, errors: list[str]) -> Index | None:
    """Parse the optional "index" object."""
    if value is None:
        return None
    data = _mapping(
        value, "index", ("name", "period", "coefficient", "loss_method"), errors
    )
    if data is None:
        return None
    name = _enum(IndexName, data.get("name"), "index.name", errors)
    period = _enum(IndexPeriod, data.get("period"), "index.period", errors)
    coefficient = _decimal(data.get("coefficient"), "index.coefficient", errors)
    if coefficient is not None and coefficient <= 0:
        errors.append("index.coefficient: must be greater than zero")
        coefficient = None
    raw_method = data.get("loss_method")
    loss_method = (
        LossMethod.ENERGY
        if raw_method is None
        else _enum(LossMethod, raw_method, "index.loss_method", errors)
    )
    if name is None or period is None or coefficient is None or loss_method is None:
        return None
    return Index(name, period, coefficient, loss_method)


def _parse_energy_price(
    value: Any, time_bands: TimeBands | None, errors: list[str]
) -> dict[str, Decimal] | None:
    """Parse the optional "energy_price" object against the time bands."""
    if value is None:
        return None
    data = _mapping(value, "energy_price", _ALL_BAND_KEYS, errors)
    if data is None:
        return None
    valid = True
    if time_bands is not None and set(data) != set(BAND_KEYS[time_bands]):
        expected = ", ".join(BAND_KEYS[time_bands])
        errors.append(
            f"energy_price: expected exactly {expected} for time_bands '{time_bands}'"
        )
        valid = False
    prices: dict[str, Decimal] = {}
    for key, raw in data.items():
        price = _decimal(raw, f"energy_price.{key}", errors)
        if price is None:
            valid = False
        else:
            prices[key] = price
    return prices if valid else None


def _parse_dispatching(value: Any, errors: list[str]) -> Dispatching | None:
    """Parse the required "dispatching" object."""
    data = _mapping(
        value, "dispatching", ("mode", "fixed_value", "capacity_market"), errors
    )
    if data is None:
        return None
    mode = _enum(DispatchingMode, data.get("mode"), "dispatching.mode", errors)
    raw_fixed = data.get("fixed_value")
    fixed_value = _decimal(raw_fixed, "dispatching.fixed_value", errors, nullable=True)
    capacity_market = _bool(
        data.get("capacity_market"), "dispatching.capacity_market", errors
    )
    valid = raw_fixed is None or fixed_value is not None
    if mode is DispatchingMode.FIXED and raw_fixed is None:
        errors.append("dispatching.fixed_value: required when mode is 'fixed'")
        valid = False
    if mode is not None and mode is not DispatchingMode.FIXED and raw_fixed is not None:
        errors.append("dispatching.fixed_value: must be null unless mode is 'fixed'")
        valid = False
    if mode is None or capacity_market is None or not valid:
        return None
    return Dispatching(mode, fixed_value, capacity_market)


def _parse_green_energy(value: Any, errors: list[str]) -> GreenEnergy | None:
    """Parse the optional "green_energy" object."""
    if value is None:
        return None
    data = _mapping(value, "green_energy", ("eur_kwh", "eur_year", "optional"), errors)
    if data is None:
        return None
    count = len(errors)
    eur_kwh = _decimal(
        data.get("eur_kwh"),
        "green_energy.eur_kwh",
        errors,
        nullable=True,
        non_negative=True,
    )
    eur_year = _decimal(
        data.get("eur_year"),
        "green_energy.eur_year",
        errors,
        nullable=True,
        non_negative=True,
    )
    optional = _bool(data.get("optional"), "green_energy.optional", errors)
    if optional is None or len(errors) > count:
        return None
    return GreenEnergy(eur_kwh, eur_year, optional)


def _parse_discounts(value: Any, errors: list[str]) -> tuple[Discount, ...]:
    """Parse the optional "discounts" list."""
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append("discounts: must be a list")
        return ()
    discounts: list[Discount] = []
    for position, item in enumerate(value):
        path = f"discounts[{position}]"
        data = _mapping(
            item, path, ("name", "kind", "value", "condition", "valid_months"), errors
        )
        if data is None:
            continue
        name = _string(data.get("name"), f"{path}.name", errors)
        kind = _enum(DiscountKind, data.get("kind"), f"{path}.kind", errors)
        amount = _decimal(data.get("value"), f"{path}.value", errors)
        condition = _string(
            data.get("condition"), f"{path}.condition", errors, nullable=True
        )
        valid_months = data.get("valid_months")
        if valid_months is not None and (
            type(valid_months) is not int or valid_months < 1
        ):
            errors.append(f"{path}.valid_months: must be a positive integer or null")
            continue
        if name is None or kind is None or amount is None:
            continue
        discounts.append(Discount(name, kind, amount, condition, valid_months))
    return tuple(discounts)


def _parse_notes(value: Any, errors: list[str]) -> tuple[str, ...]:
    """Parse the optional "notes" list."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(note, str) for note in value):
        errors.append("notes: must be a list of strings")
        return ()
    return tuple(value)


def _check_price_rules(
    root: dict[str, Any],
    price_type: PriceType,
    energy_price: dict[str, Decimal] | None,
    dispatching: Dispatching | None,
    errors: list[str],
) -> None:
    """Apply the rules that tie index, prices and dispatching to price_type."""
    has_index = root.get("index") is not None
    has_price = root.get("energy_price") is not None

    if price_type is PriceType.INDEXED and not has_index:
        errors.append("index: required when price_type is 'indexed'")
    if price_type is not PriceType.INDEXED and has_index:
        errors.append(f"index: must be null when price_type is '{price_type}'")

    if price_type is PriceType.FIXED:
        if not has_price:
            errors.append("energy_price: required when price_type is 'fixed'")
        elif energy_price and any(price <= 0 for price in energy_price.values()):
            errors.append(
                "energy_price: prices must be greater than zero "
                "when price_type is 'fixed'"
            )
    if price_type is PriceType.REGULATED and has_price:
        errors.append("energy_price: must be null when price_type is 'regulated'")

    if dispatching is not None:
        is_tutela = dispatching.mode is DispatchingMode.TUTELA
        if is_tutela != (price_type is PriceType.REGULATED):
            errors.append(
                "dispatching.mode: 'tutela' goes with price_type 'regulated', "
                "and only with it"
            )


def _parse_contract(data: Any, errors: list[str]) -> Contract | None:
    """Parse the whole document, collecting every error found."""
    root = _mapping(data, "", _CONTRACT_KEYS, errors)
    if root is None:
        return None

    version = root.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        errors.append(
            f"schema_version: unsupported value {version!r} (expected {SCHEMA_VERSION})"
        )

    supplier = _string(root.get("supplier"), "supplier", errors)
    offer_name = _string(root.get("offer_name"), "offer_name", errors, nullable=True)
    offer_code = _string(root.get("offer_code"), "offer_code", errors, nullable=True)
    price_type = _enum(PriceType, root.get("price_type"), "price_type", errors)
    time_bands = _enum(TimeBands, root.get("time_bands"), "time_bands", errors)
    index = _parse_index(root.get("index"), errors)
    energy_price = _parse_energy_price(root.get("energy_price"), time_bands, errors)
    dispatching = _parse_dispatching(root.get("dispatching"), errors)
    disp_bt = _bool(root.get("disp_bt"), "disp_bt", errors, nullable=True)
    fixed_fee = _decimal(
        root.get("fixed_fee_eur_year"),
        "fixed_fee_eur_year",
        errors,
        nullable=True,
        non_negative=True,
    )
    energy_fee = _decimal(
        root.get("energy_fee_eur_kwh"),
        "energy_fee_eur_kwh",
        errors,
        nullable=True,
        non_negative=True,
    )
    green_energy = _parse_green_energy(root.get("green_energy"), errors)
    discounts = _parse_discounts(root.get("discounts"), errors)
    notes = _parse_notes(root.get("notes"), errors)

    if price_type is not None:
        _check_price_rules(root, price_type, energy_price, dispatching, errors)

    if (
        errors
        or supplier is None
        or price_type is None
        or time_bands is None
        or dispatching is None
    ):
        return None
    return Contract(
        supplier=supplier,
        price_type=price_type,
        time_bands=time_bands,
        dispatching=dispatching,
        offer_name=offer_name,
        offer_code=offer_code,
        index=index,
        energy_price=energy_price,
        disp_bt=disp_bt,
        fixed_fee_eur_year=fixed_fee,
        energy_fee_eur_kwh=energy_fee,
        green_energy=green_energy,
        discounts=discounts,
        notes=notes,
    )
