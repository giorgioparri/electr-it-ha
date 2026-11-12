# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinator: feeds the engine with meter readings and market values.

It listens to the grid import meters, keeps the monthly counters by band,
saves them to disk, and recomputes prices and the month so far whenever a
meter changes and at least once a minute.

At the month change it bills the month that just ended. A market value read
from an entity that reports its month (the `month` attribute, as the GME
account sensors do) is used only once it refers to the month being billed
and is complete; for a sensor of the running month, its twin of the month
before is used. Other entities and fixed numbers are used as they are. If
the values are still not there after a few days, the month is billed with
what is available.
"""

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    ATTR_MONTH,
    CONF_CONTRACT,
    CONF_MARKET,
    CONF_METERS,
    CONF_POWER_KW,
    CONF_RESIDENT,
    DOMAIN,
    SAVE_DELAY_SECONDS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    UPDATE_INTERVAL_SECONDS,
)
from .engine.bands import ITALY, Band, band_at, contract_band
from .engine.calculator import Bill, CalculationError, Period, Supply, compute_bill
from .engine.contract import Contract
from .engine.counters import Counters, MonthEnergy
from .engine.estimate import (
    Market,
    UnitPrice,
    fixed_cost_per_day,
    month_to_date,
    unit_price,
)
from .engine.market import build_market, to_eur_per_kwh
from .engine.regulated import RegulatedDataError

_LOGGER = logging.getLogger(__name__)

# Days after the month change after which a month is billed anyway.
CLOSE_WAIT_DAYS = 3
_CURRENT_SUFFIX = "_current"
_PREVIOUS_SUFFIX = "_previous"

type ElectrIthaConfigEntry = ConfigEntry[ElectrIthaCoordinator]


@dataclass(frozen=True, slots=True)
class ClosedMonth:
    """The bill of a month that ended, as computed at the month change."""

    energy: MonthEnergy
    taxable: Decimal | None
    vat: Decimal | None
    problem: str | None = None

    @property
    def total(self) -> Decimal | None:
        """Amount due with VAT, or None when the month could not be billed."""
        if self.taxable is None or self.vat is None:
            return None
        return self.taxable + self.vat

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "energy": self.energy.to_dict(),
            "taxable": None if self.taxable is None else str(self.taxable),
            "vat": None if self.vat is None else str(self.vat),
            "problem": self.problem,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Rebuild a closed month from `to_dict` output."""
        taxable, vat = data.get("taxable"), data.get("vat")
        return cls(
            MonthEnergy.from_dict(data["energy"]),
            None if taxable is None else Decimal(taxable),
            None if vat is None else Decimal(vat),
            data.get("problem"),
        )


@dataclass(frozen=True, slots=True)
class ElectrIthaData:
    """What the sensors show. A None value has its reason in `problems`."""

    moment: datetime
    band: Band
    energy: MonthEnergy
    unit_price: UnitPrice | None
    fixed_cost_per_day: Decimal | None
    month_bill: Bill | None
    closed_month: ClosedMonth | None
    pending_month: date | None
    estimated: bool
    problems: tuple[str, ...]


class ElectrIthaCoordinator(DataUpdateCoordinator[ElectrIthaData]):
    """Keep counters and computed values for one supply."""

    config_entry: ElectrIthaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ElectrIthaConfigEntry) -> None:
        """Read the configuration; nothing is loaded or subscribed yet."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.contract = Contract.from_dict(entry.data[CONF_CONTRACT])
        self.supply = Supply(
            resident=entry.data[CONF_RESIDENT],
            power_kw=Decimal(str(entry.data[CONF_POWER_KW])),
        )
        self._meters: list[str] = list(entry.data[CONF_METERS])
        self._market_sources: dict[str, str | float] = dict(entry.data[CONF_MARKET])
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )
        self._counters = Counters(dt_util.now())
        self._closed_month: ClosedMonth | None = None
        self._unsubscribe: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Load the saved counters and start listening to the meters."""
        saved = await self._store.async_load()
        if saved:
            self._counters = Counters.from_dict(saved["counters"])
            if saved.get("closed_month"):
                self._closed_month = ClosedMonth.from_dict(saved["closed_month"])
        self._counters.keep_meters(self._meters)
        now = dt_util.now()
        self._roll(now)
        self._try_close(now)
        for meter in self._meters:
            self._record(meter, self.hass.states.get(meter), now)
        self._unsubscribe = async_track_state_change_event(
            self.hass, self._meters, self._handle_meter
        )

    async def async_shutdown(self) -> None:
        """Stop listening and save the counters right away."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self._store.async_save(self._data_to_save())
        await super().async_shutdown()

    async def _async_update_data(self) -> ElectrIthaData:
        """Recompute on the timer, so prices follow band and month changes."""
        now = dt_util.now()
        self._roll(now)
        self._try_close(now)
        return self._compute(now)

    @callback
    def _handle_meter(self, event: Event[EventStateChangedData]) -> None:
        """Take a new meter reading and publish the updated values."""
        now = dt_util.now()
        self._roll(now)
        added = self._record(event.data["entity_id"], event.data["new_state"], now)
        if added:
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)
            self.async_set_updated_data(self._compute(now))

    def _record(self, meter: str, state: State | None, now: datetime) -> Decimal:
        """Feed one meter state to the counters; return the kWh it added."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return Decimal(0)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        try:
            reading = EnergyConverter.convert(
                float(state.state), unit, UnitOfEnergy.KILO_WATT_HOUR
            )
        except (ValueError, TypeError, HomeAssistantError):
            _LOGGER.warning(
                "Ignoring %s: state '%s' with unit '%s' is not an energy reading",
                meter,
                state.state,
                unit,
            )
            return Decimal(0)
        return self._counters.record(meter, Decimal(str(reading)), now)

    def _roll(self, now: datetime) -> None:
        """At the month change, forget the bill of the month before."""
        if self._counters.roll(now):
            self._closed_month = None
            self._try_close(now)
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)

    def _pending_month(self) -> MonthEnergy | None:
        """Return the month that ended and has no bill yet."""
        ended = self._counters.previous
        if ended is None:
            return None
        closed = self._closed_month
        if closed is not None and closed.energy.month == ended.month:
            return None
        return ended

    def _try_close(self, now: datetime) -> None:
        """Bill the month that ended, once its market values are there."""
        ended = self._pending_month()
        if ended is None:
            return
        waited = now.astimezone(ITALY).date() - self._counters.current.month
        values, ready = self._read_values(ended.month)
        if not ready and waited < timedelta(days=CLOSE_WAIT_DAYS):
            return
        if not ready:
            _LOGGER.warning(
                "Billing %s with the market values available: some never "
                "reported that month",
                f"{ended.month:%Y-%m}",
            )
        self._closed_month = self._close(ended, values)
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)

    def _close(self, energy: MonthEnergy, values: dict[str, Decimal]) -> ClosedMonth:
        """Bill a whole month with the given market values."""
        last_day = calendar.monthrange(energy.month.year, energy.month.month)[1]
        try:
            market = build_market(self.contract, values)
            bill = compute_bill(
                self.contract,
                self.supply,
                [
                    Period(
                        start=energy.month,
                        end=energy.month.replace(day=last_day),
                        kwh=energy.by_contract_band(self.contract.time_bands),
                        pun=market.pun,
                        dispatching=market.dispatching,
                        capacity=market.capacity,
                        cdispd=market.cdispd,
                    )
                ],
            )
        except (CalculationError, RegulatedDataError) as err:
            _LOGGER.warning("Could not bill %s: %s", energy.month.isoformat(), err)
            return ClosedMonth(energy, None, None, str(err))
        return ClosedMonth(energy, bill.taxable, bill.vat)

    def _market(self) -> Market:
        """Read the market values for the running month."""
        return build_market(self.contract, self._read_values(None)[0])

    def _read_values(self, month: date | None) -> tuple[dict[str, Decimal], bool]:
        """Read the market values from their entities or fixed numbers.

        With a `month`, prefer states that report that month, and say whether
        every entity that reports a month had it, complete.
        """
        values: dict[str, Decimal] = {}
        ready = True
        for name, source in self._market_sources.items():
            if not isinstance(source, str):
                values[name] = Decimal(str(source))
                continue
            state = self.hass.states.get(source)
            if month is not None and state is not None:
                state, found = self._state_for_month(source, state, month)
                ready = ready and found
            value = self._state_value(state)
            if value is not None:
                values[name] = value
        return values, ready

    def _state_for_month(
        self, entity_id: str, state: State, month: date
    ) -> tuple[State, bool]:
        """Return the state of `entity_id`, or of its twin, that refers to `month`.

        The second value is False when the entity reports a month but neither
        it nor its twin reports `month`, complete.
        """
        wanted = f"{month:%Y-%m}"
        if ATTR_MONTH not in state.attributes:
            return state, True
        candidates = [state]
        twin = self._previous_twin(entity_id)
        if twin is not None:
            candidates.append(twin)
        for candidate in candidates:
            if candidate.attributes.get(ATTR_MONTH) == wanted:
                return candidate, candidate.attributes.get("complete", True) is True
        return state, False

    def _previous_twin(self, entity_id: str) -> State | None:
        """Return the state of the last-month twin of an electrITHA sensor."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if (
            entry is None
            or entry.platform != DOMAIN
            or not entry.unique_id.endswith(_CURRENT_SUFFIX)
        ):
            return None
        unique_id = entry.unique_id.removesuffix(_CURRENT_SUFFIX) + _PREVIOUS_SUFFIX
        twin = registry.async_get_entity_id(entry.domain, DOMAIN, unique_id)
        return None if twin is None else self.hass.states.get(twin)

    @staticmethod
    def _state_value(state: State | None) -> Decimal | None:
        """Return a state as EUR/kWh, or None when it has no usable number."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = Decimal(state.state)
        except InvalidOperation:
            return None
        if not value.is_finite():
            return None
        return to_eur_per_kwh(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

    def _compute(self, now: datetime) -> ElectrIthaData:
        """Compute everything the sensors show for the instant `now`."""
        day: date = now.astimezone(ITALY).date()
        band = band_at(now)
        energy = self._counters.current
        pending = self._pending_month()
        problems: list[str] = []
        price: UnitPrice | None = None
        fixed: Decimal | None = None
        bill: Bill | None = None

        try:
            fixed = fixed_cost_per_day(self.contract, self.supply, day)
        except RegulatedDataError as err:
            problems.append(str(err))
        try:
            market = self._market()
            price = unit_price(
                self.contract,
                self.supply,
                market,
                day,
                contract_band(band, self.contract.time_bands),
                energy.total,
            )
            bill = month_to_date(
                self.contract,
                self.supply,
                market,
                day,
                energy.by_contract_band(self.contract.time_bands),
            )
        except (CalculationError, RegulatedDataError) as err:
            if str(err) not in problems:
                problems.append(str(err))

        return ElectrIthaData(
            moment=now,
            band=band,
            energy=energy,
            unit_price=price,
            fixed_cost_per_day=fixed,
            month_bill=bill,
            closed_month=self._closed_month,
            pending_month=None if pending is None else pending.month,
            estimated=bool(self._market_sources),
            problems=tuple(problems),
        )

    def _data_to_save(self) -> dict[str, Any]:
        """Return what goes to disk."""
        closed = self._closed_month
        return {
            "counters": self._counters.to_dict(),
            "closed_month": None if closed is None else closed.to_dict(),
        }
