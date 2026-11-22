# SPDX-License-Identifier: GPL-3.0-or-later
"""Electricity offers of the Portale Offerte (ARERA, Acquirente Unico).

Reads the open data file of the free market offers and converts each offer
to a supply contract. The format is Acquirente Unico's "Funzionamento e
Specifiche del Processo di Trasmissione Offerte Mercato Retail", v5.0.

The conversion follows what the Portale itself takes into account to work out
the yearly spend, mapped onto the contract model:

- every company component in EUR/kWh, whatever its macro area, is a price per
  kWh: summed by band, it is the energy price of a fixed offer and the spread
  of an indexed one. The Portale states them with losses, as the model does;
- every component in EUR/year is summed into the yearly fixed fee;
- the index is the PUN Index GME. The Portale does not say how losses are
  billed on it: the standard factor is assumed, in the price, unless the
  offer states a coefficient above 1.

What the model cannot hold makes the conversion partial (the closest valid
contract, with the reasons) or unsupported (no contract).

Pure Python: no Home Assistant imports are allowed in this package.
"""

import json
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from .calculator import STANDARD_LOSS_FACTOR
from .contract import SCHEMA_VERSION, Contract, ContractError

NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
_NS = f"{{{NAMESPACE}}}"

_LOSSES = 1 + STANDARD_LOSS_FACTOR
_DATE_FORMAT = "%d/%m/%Y_%H:%M:%S"
_MICRO = Decimal("0.000001")
_ZERO = Decimal(0)

# Codes of the specification.
_ELECTRICITY = "01"
_DOMESTIC = "01"
_FIXED, _INDEXED = "01", "02"
_OFFER_TYPES = {"03": "FLAT", "04": "mixed (fixed and variable)"}
_PUN_PERIODS = {"01": "quarterly", "08": "bimonthly", "12": "monthly"}
_RESIDENCE = {"01": "resident", "02": "non_resident"}

# TIPOLOGIA_FASCE -> time bands of the model, and FASCIA_COMPONENTE -> key.
_BANDS: dict[str, tuple[str, dict[str, str]]] = {
    "01": ("mono", {"01": "f0"}),
    "03": ("f1_f2_f3", {"01": "f1", "02": "f2", "03": "f3"}),
    "91": ("f1_f23", {"01": "f1", "91": "f23"}),
}
_BAND_NAMES = {
    "02": "F1, F2",
    "04": "F1-F4",
    "05": "F1-F5",
    "06": "F1-F6",
    "07": "Peak/OffPeak",
    "92": "F2 / F1+F3",
    "93": "F3 / F1+F2",
}

# Company components.
_GREEN = "06"
_ONE_OFF_AREA = "05"
_OPTIONAL = "02"
_PER_YEAR, _PER_KW, _PER_KWH, _PER_SMC, _EUR, _PERCENT = (
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
)

# Dispatching codes.
_DISP = "01"
_DISP_PD = "02"
_CAPACITY = {"09", "10"}
_DISP_BT = "13"
_CDISPD = "14"
_OWN = "99"
_REGULATED = {"01": "PCV", "02": "PPE"}

# Discounts.
_CONDITIONS = {
    "01": "electronic billing",
    "02": "online account management",
    "03": "electronic billing and direct debit",
}
_DISCOUNT_KINDS = {
    _PER_YEAR: "per_year",
    _PER_KWH: "per_kwh",
    _EUR: "one_off",
    _PERCENT: "percent",
}
_DISCOUNT_POWER = "02"
_DISCOUNT_TUTELA = "04"


class Status(StrEnum):
    """How much of an offer the contract model holds."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class OfferInfo:
    """What identifies an offer and who can take it, as the Portale states."""

    code: str
    name: str
    vat_number: str
    supplier: str
    supplier_url: str | None
    offer_url: str | None
    domestic: bool
    residence: str
    single: bool
    valid_from: datetime | None
    valid_to: datetime | None
    duration_months: int | None
    power_min_kw: Decimal | None
    power_max_kw: Decimal | None
    consumption_min_kwh: int | None
    consumption_max_kwh: int | None
    regions: tuple[str, ...]
    provinces: tuple[str, ...]
    municipalities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One IntervalloPrezzi of a company component."""

    band: str | None
    price: Decimal
    unit: str
    tiered: bool
    timed: bool


@dataclass(frozen=True, slots=True)
class Component:
    """One ComponenteImpresa."""

    name: str
    kind: str
    area: str
    intervals: tuple[PriceInterval, ...]


@dataclass(frozen=True, slots=True)
class DiscountPrice:
    """One PrezziSconto."""

    kind: str
    unit: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class SourceDiscount:
    """One Sconto, as stated."""

    name: str
    condition: str | None
    condition_text: str | None
    duration_months: int | None
    prices: tuple[DiscountPrice, ...]


@dataclass(frozen=True, slots=True)
class Offer:
    """One offer of the Portale, read but not yet interpreted."""

    info: OfferInfo
    market: str
    offer_type: str
    all_inclusive: str | None
    indices: tuple[tuple[str, Decimal | None], ...]
    time_bands: str | None
    dispatching: tuple[tuple[str, Decimal | None, str], ...]
    regulated: tuple[str, ...]
    components: tuple[Component, ...]
    discounts: tuple[SourceDiscount, ...]


@dataclass(frozen=True, slots=True)
class Conversion:
    """An offer converted to a contract, with what could not be converted.

    `problems` says what makes the conversion partial or unsupported;
    `warnings` is reserved for data that looks wrong, and is empty for now.
    """

    info: OfferInfo
    status: Status
    contract: Contract | None
    problems: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def contract_json(self) -> str | None:
        """Return the contract as the JSON the contract screen shows."""
        if self.contract is None:
            return None
        return json.dumps(self.contract.to_dict(), indent=2, ensure_ascii=False)


# --- Reading ------------------------------------------------------------------


def parse_offers(source: str | Path | BinaryIO) -> Iterator[Offer]:
    """Read the offers of a Portale file one at a time, in order.

    The file is streamed: each offer is dropped once read, so the 20 MB of a
    daily file never sit in memory as a tree.
    """
    root: ET.Element | None = None
    for event, element in ET.iterparse(source, events=("start", "end")):
        if event == "start":
            if root is None:
                root = element
            continue
        if element.tag == f"{_NS}offerta":
            yield _read_offer(element)
            if root is not None:
                root.clear()


def _text(element: ET.Element | None, path: str) -> str | None:
    """Return the stripped text at `path`, or None when absent or empty."""
    if element is None:
        return None
    found = element.find(_path(path))
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _texts(element: ET.Element | None, path: str) -> tuple[str, ...]:
    """Return every non-empty text at `path`."""
    if element is None:
        return ()
    values = (
        found.text.strip() for found in element.iterfind(_path(path)) if found.text
    )
    return tuple(value for value in values if value)


def _path(path: str) -> str:
    """Qualify every step of a path with the namespace."""
    return "/".join(f"{_NS}{step}" for step in path.split("/"))


def _decimal(value: str | None) -> Decimal | None:
    """Read a number, or None."""
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _integer(value: str | None) -> int | None:
    """Read a whole number, or None."""
    number = _decimal(value)
    return None if number is None else int(number)


def _date(value: str | None) -> datetime | None:
    """Read a timestamp of the Portale."""
    if value is None:
        return None
    try:
        return datetime.strptime(value, _DATE_FORMAT)
    except ValueError:
        return None


def supplier_domain(url: str | None) -> str:
    """Return the domain of the supplier's site, without scheme and www."""
    if not url:
        return ""
    text = url.strip()
    if "://" not in text:
        text = f"//{text}"
    host = (urlsplit(text).hostname or "").lower()
    return host.removeprefix("www.")


def _read_info(offer: ET.Element) -> OfferInfo:
    """Read what identifies the offer and who can take it."""
    detail = offer.find(_path("DettaglioOfferta"))
    duration = _integer(_text(detail, "DURATA"))
    site = _text(detail, "Contatti/URL_SITO_VENDITORE")
    zones = offer.find(_path("ZoneOfferta"))
    features = offer.find(_path("CaratteristicheOfferta"))
    return OfferInfo(
        code=_text(offer, "IdentificativiOfferta/COD_OFFERTA") or "",
        name=_text(detail, "NOME_OFFERTA") or "",
        vat_number=_text(offer, "IdentificativiOfferta/PIVA_UTENTE") or "",
        supplier=supplier_domain(site),
        supplier_url=site,
        offer_url=_text(detail, "Contatti/URL_OFFERTA"),
        domestic=_text(detail, "TIPO_CLIENTE") == _DOMESTIC,
        residence=_RESIDENCE.get(_text(detail, "DOMESTICO_RESIDENTE") or "", "all"),
        single=_text(detail, "OFFERTA_SINGOLA") != "NO",
        valid_from=_date(_text(offer, "ValiditaOfferta/DATA_INIZIO")),
        valid_to=_date(_text(offer, "ValiditaOfferta/DATA_FINE")),
        duration_months=None if duration is None or duration < 0 else duration,
        power_min_kw=_decimal(_text(features, "POTENZA_MIN")),
        power_max_kw=_decimal(_text(features, "POTENZA_MAX")),
        consumption_min_kwh=_integer(_text(features, "CONSUMO_MIN")),
        consumption_max_kwh=_integer(_text(features, "CONSUMO_MAX")),
        regions=_texts(zones, "REGIONE"),
        provinces=_texts(zones, "PROVINCIA"),
        municipalities=_texts(zones, "COMUNE"),
    )


def _read_component(element: ET.Element) -> Component:
    """Read one ComponenteImpresa."""
    name = _text(element, "NOME") or ""
    description = _text(element, "DESCRIZIONE") or ""
    label = name
    if (
        description
        and description != name
        and (not name or name == "ComponenteImpresa")
    ):
        label = description
    elif description and description != name and len(description) <= 60:
        label = f"{name} ({description})"
    intervals: list[PriceInterval] = []
    for interval in element.iterfind(_path("IntervalloPrezzi")):
        price = _decimal(_text(interval, "PREZZO"))
        if price is None:
            continue
        tiered = (
            _text(interval, "CONSUMO_DA") is not None
            or _text(interval, "CONSUMO_A") is not None
        )
        timed = interval.find(_path("PeriodoValidita")) is not None
        intervals.append(
            PriceInterval(
                band=_text(interval, "FASCIA_COMPONENTE"),
                price=price,
                unit=_text(interval, "UNITA_MISURA") or "",
                tiered=tiered,
                timed=timed,
            )
        )
    return Component(
        name=label,
        kind=_text(element, "TIPOLOGIA") or "",
        area=_text(element, "MACROAREA") or "",
        intervals=tuple(intervals),
    )


def _read_discount(element: ET.Element) -> SourceDiscount:
    """Read one Sconto."""
    prices = tuple(
        DiscountPrice(
            kind=_text(price, "TIPOLOGIA") or "",
            unit=_text(price, "UNITA_MISURA") or "",
            price=_decimal(_text(price, "PREZZO")) or _ZERO,
        )
        for price in element.iterfind(_path("PrezziSconto"))
    )
    return SourceDiscount(
        name=_text(element, "NOME") or "",
        condition=_text(element, "Condizione/CONDIZIONE_APPLICAZIONE"),
        condition_text=_text(element, "Condizione/DESCRIZIONE_CONDIZIONE"),
        duration_months=_integer(_text(element, "PeriodoValidita/DURATA")),
        prices=prices,
    )


def _read_offer(offer: ET.Element) -> Offer:
    """Read one offer element."""
    detail = offer.find(_path("DettaglioOfferta"))
    indices = tuple(
        (_text(ref, "IDX_PREZZO_ENERGIA") or "", _decimal(_text(ref, "COEFFICIENTE")))
        for ref in offer.iterfind(_path("RiferimentiPrezzoEnergia"))
    )
    dispatching = tuple(
        (
            _text(item, "TIPO_DISPACCIAMENTO") or "",
            _decimal(_text(item, "VALORE_DISP")),
            _text(item, "NOME") or "",
        )
        for item in offer.iterfind(_path("Dispacciamento"))
    )
    regulated = tuple(
        code
        for block in offer.iterfind(_path("ComponentiRegolate"))
        for code in _texts(block, "CODICE")
    )
    return Offer(
        info=_read_info(offer),
        market=_text(detail, "TIPO_MERCATO") or "",
        offer_type=_text(detail, "TIPO_OFFERTA") or "",
        all_inclusive=_text(detail, "OFFERTA_ONNICOMPRENSIVA"),
        indices=indices,
        time_bands=_text(offer, "TipoPrezzo/TIPOLOGIA_FASCE"),
        dispatching=dispatching,
        regulated=regulated,
        components=tuple(
            _read_component(item) for item in offer.iterfind(_path("ComponenteImpresa"))
        ),
        discounts=tuple(
            _read_discount(item) for item in offer.iterfind(_path("Sconto"))
        ),
    )


# --- Conversion -----------------------------------------------------------------


class _UnsupportedError(Exception):
    """The offer cannot be held by the contract model."""


def convert(offer: Offer) -> Conversion:
    """Convert an offer to a contract; never raises."""
    problems: list[str] = []
    notes: list[str] = [
        f"Imported from the Portale Offerte (ARERA) open data, offer {offer.info.code}."
    ]
    try:
        data = _contract_data(offer, problems, notes)
    except _UnsupportedError as err:
        return Conversion(offer.info, Status.UNSUPPORTED, None, (str(err),))
    try:
        contract = Contract.from_dict(data)
    except ContractError as err:
        return Conversion(offer.info, Status.UNSUPPORTED, None, tuple(err.errors))
    status = Status.PARTIAL if problems else Status.COMPLETE
    return Conversion(offer.info, status, contract, tuple(problems))


def _contract_data(
    offer: Offer, problems: list[str], notes: list[str]
) -> dict[str, Any]:
    """Build the contract document, or raise _UnsupportedError."""
    if offer.market != _ELECTRICITY:
        raise _UnsupportedError("not an electricity offer")
    if offer.offer_type in _OFFER_TYPES:
        raise _UnsupportedError(
            f"{_OFFER_TYPES[offer.offer_type]} offers are not supported"
        )
    if offer.offer_type not in (_FIXED, _INDEXED):
        raise _UnsupportedError(f"offer type {offer.offer_type!r} is not supported")
    if offer.all_inclusive:
        raise _UnsupportedError("all-inclusive offers are not supported")

    indexed = offer.offer_type == _INDEXED
    index = _index(offer, problems, notes) if indexed else None
    time_bands, band_keys = _time_bands(offer, problems)
    prices, fee, green = _components(offer, band_keys, problems, notes)
    if not indexed and not prices:
        raise _UnsupportedError("no energy price is stated")
    energy_price = {key: prices.get(key, _ZERO) for key in band_keys.values()}
    if time_bands == "mono" and set(band_keys.values()) != {"f0"}:
        values = list(energy_price.values())
        energy_price = {"f0": sum(values, _ZERO) / len(values)}
    dispatching, disp_bt = _dispatching(offer, problems, notes)

    for code in offer.regulated:
        name = _REGULATED.get(code, code)
        problems.append(
            f"the regulated component {name} applied by the supplier is not in "
            "the contract"
        )
    discounts = _discounts(offer, notes)

    return {
        "schema_version": SCHEMA_VERSION,
        "supplier": offer.info.supplier or offer.info.vat_number,
        "offer_name": offer.info.name or None,
        "offer_code": offer.info.code or None,
        "price_type": "indexed" if indexed else "fixed",
        "time_bands": time_bands,
        "index": index,
        "energy_price": energy_price,
        "dispatching": dispatching,
        "disp_bt": disp_bt,
        "fixed_fee_eur_year": fee if fee else None,
        "energy_fee_eur_kwh": None,
        "green_energy": green,
        "discounts": discounts,
        "notes": notes,
    }


def _index(offer: Offer, problems: list[str], notes: list[str]) -> dict[str, Any]:
    """Map the reference index and its loss coefficient."""
    if len(offer.indices) != 1:
        if not offer.indices:
            raise _UnsupportedError("an indexed offer with no index")
        raise _UnsupportedError("a sum of indices is not supported")
    code, coefficient = offer.indices[0]
    if code not in _PUN_PERIODS:
        raise _UnsupportedError(f"index {code!r} is not the PUN Index GME")
    if coefficient is None or coefficient == 1:
        coefficient = _LOSSES
        notes.append(
            f"Network losses: the Portale does not state them; the standard "
            f"{_LOSSES} is assumed, billed in the price. If your bill shows "
            "losses as separate kWh, set index.loss_method to energy."
        )
    elif coefficient > 1:
        notes.append(
            f"Network losses: coefficient {coefficient} as stated on the Portale, "
            "billed in the price. If your bill shows losses as separate kWh, set "
            "index.loss_method to energy."
        )
    else:
        scaled = coefficient * _LOSSES
        problems.append(
            f"coefficient {coefficient} on the index is not a loss factor: "
            f"multiplied by the standard losses, {scaled}"
        )
        coefficient = scaled
    return {
        "name": "pun_index_gme",
        "period": _PUN_PERIODS[code],
        "coefficient": coefficient,
        "loss_method": "price",
    }


def _time_bands(offer: Offer, problems: list[str]) -> tuple[str, dict[str, str]]:
    """Map the time bands; unknown ones become a single averaged price."""
    code = offer.time_bands or "01"
    if code in _BANDS:
        return _BANDS[code]
    name = _BAND_NAMES.get(code, code)
    problems.append(
        f"time bands {name} are not supported: one price for every hour, the "
        "average of the bands"
    )
    bands = sorted(
        {
            interval.band
            for component in offer.components
            for interval in component.intervals
            if interval.band
        }
    )
    return "mono", {band: band for band in bands} or {"01": "f0"}


def _pick(
    component: Component, unit: str, problems: list[str], notes: list[str]
) -> list[PriceInterval]:
    """Return the intervals of one unit that apply, flattening tiers and periods."""
    intervals = [interval for interval in component.intervals if interval.unit == unit]
    special = [i for i in intervals if i.tiered or i.timed]
    if not special:
        return intervals
    plain = [i for i in intervals if not (i.tiered or i.timed)]
    what = "consumption tiers" if any(i.tiered for i in special) else "prices by period"
    by_band: dict[str | None, set[Decimal]] = {}
    for interval in intervals:
        by_band.setdefault(interval.band, set()).add(interval.price)
    if all(len(prices) == 1 for prices in by_band.values()):
        notes.append(
            f"{component.name}: the same price in all its {what}, used throughout."
        )
        return plain or _first_by_band(intervals)
    problems.append(
        f"{component.name}: {what} are not supported; the default price is used"
    )
    return plain or _first_by_band(intervals)


def _first_by_band(intervals: Iterable[PriceInterval]) -> list[PriceInterval]:
    """Keep the first interval of each band."""
    seen: dict[str | None, PriceInterval] = {}
    for interval in intervals:
        seen.setdefault(interval.band, interval)
    return list(seen.values())


def _components(
    offer: Offer, band_keys: dict[str, str], problems: list[str], notes: list[str]
) -> tuple[dict[str, Decimal], Decimal, dict[str, Any] | None]:
    """Sum the company components into prices by band and a yearly fee."""
    prices: dict[str, Decimal] = {}
    fee = _ZERO
    per_kwh: list[str] = []
    per_year: list[str] = []
    green: dict[str, Any] | None = None
    for component in offer.components:
        units = {interval.unit for interval in component.intervals}
        if component.area == _ONE_OFF_AREA or units <= {_EUR}:
            amounts = ", ".join(f"{i.price} EUR" for i in component.intervals)
            notes.append(f"One-off: {component.name}, {amounts}.")
            continue
        if component.area == _GREEN and component.kind == _OPTIONAL:
            kwh = _pick(component, _PER_KWH, problems, notes)
            year = _pick(component, _PER_YEAR, problems, notes)
            green = {
                "eur_kwh": max((i.price for i in kwh), default=None),
                "eur_year": sum((i.price for i in year), _ZERO) if year else None,
                "optional": True,
            }
            continue
        for unit in sorted(units):
            if unit == _PER_KWH:
                for interval in _pick(component, unit, problems, notes):
                    keys = (
                        list(band_keys.values())
                        if interval.band is None
                        else [band_keys.get(interval.band, "")]
                    )
                    if keys == [""]:
                        problems.append(
                            f"{component.name}: band {interval.band} does not belong "
                            "to the offer's time bands"
                        )
                        continue
                    for key in keys:
                        prices[key] = prices.get(key, _ZERO) + interval.price
                per_kwh.append(component.name)
            elif unit == _PER_YEAR:
                fee += sum(
                    (i.price for i in _pick(component, unit, problems, notes)), _ZERO
                )
                per_year.append(component.name)
            elif unit == _PER_KW:
                amount = component.intervals[0].price
                problems.append(
                    f"{component.name}: {amount} EUR/kW per year is not supported"
                )
            elif unit == _EUR:
                amounts = ", ".join(
                    f"{i.price} EUR" for i in component.intervals if i.unit == _EUR
                )
                notes.append(f"One-off: {component.name}, {amounts}.")
            else:
                problems.append(f"{component.name}: unit {unit!r} is not supported")
    if per_kwh:
        what = "spread" if offer.offer_type == _INDEXED else "energy price"
        notes.append(f"Per kWh, summed into the {what}: {', '.join(per_kwh)}.")
    if per_year:
        notes.append(f"Per year, summed into the fixed fee: {', '.join(per_year)}.")
    return prices, fee, green


def _dispatching(
    offer: Offer, problems: list[str], notes: list[str]
) -> tuple[dict[str, Any], bool]:
    """Map the dispatching codes; return the dispatching object and disp_bt."""
    codes = [code for code, _value, _name in offer.dispatching]
    own = [(value, name) for code, value, name in offer.dispatching if code == _OWN]
    unknown = sorted(
        set(codes) - {_DISP, _DISP_PD, _DISP_BT, _CDISPD, _OWN} - _CAPACITY
    )
    for code in unknown:
        problems.append(f"dispatching code {code} is not supported")
    capacity = any(code in _CAPACITY for code in codes)

    if _DISP in codes:
        for value, name in own:
            problems.append(
                f"dispatching code 99 ({name}, {value} EUR/kWh) on top of the "
                "others is not in the contract"
            )
        mode = {"mode": "regulated", "fixed_value": None, "capacity_market": capacity}
        return mode, _DISP_BT in codes

    if set(codes) == {_OWN} and len(own) == 1 and own[0][0] is not None:
        value = (own[0][0] / _LOSSES).quantize(_MICRO)
        notes.append(
            f"Dispatching: {own[0][0]} EUR/kWh stated with losses, {value} without."
        )
        mode = {"mode": "fixed", "fixed_value": value, "capacity_market": capacity}
        return mode, _DISP_BT in codes

    if _DISP_PD in codes:
        problems.append("dispatching PD (code 02) is not supported: CdispD is used")
    elif _CDISPD not in codes:
        problems.append("no supported dispatching code: CdispD is used")
    for value, name in own:
        problems.append(
            f"dispatching code 99 ({name}, {value} EUR/kWh) on top of the others "
            "is not in the contract"
        )
    notes.append("Dispatching: the CdispD, with the DispBT billed as with the CdispD.")
    return {"mode": "cdispd", "fixed_value": None, "capacity_market": False}, True


def _discounts(offer: Offer, notes: list[str]) -> list[dict[str, Any]]:
    """Map the discounts the model holds; note the others."""
    result: list[dict[str, Any]] = []
    for discount in offer.discounts:
        if discount.condition in (None, "00"):
            condition = None
        elif discount.condition == "99":
            condition = discount.condition_text
        else:
            condition = _CONDITIONS.get(discount.condition, discount.condition)
        for price in discount.prices:
            if price.kind == _DISCOUNT_POWER:
                notes.append(f"Discount {discount.name}: a discount on power.")
                continue
            if price.kind == _DISCOUNT_TUTELA:
                notes.append(
                    f"Discount {discount.name}: a discount on the tutela prices."
                )
                continue
            kind = _DISCOUNT_KINDS.get(price.unit)
            if kind is None:
                notes.append(
                    f"Discount {discount.name}: unit {price.unit!r} is not supported."
                )
                continue
            result.append(
                {
                    "name": discount.name or "Discount",
                    "kind": kind,
                    "value": price.price,
                    "condition": condition,
                    "valid_months": discount.duration_months,
                }
            )
    return result
