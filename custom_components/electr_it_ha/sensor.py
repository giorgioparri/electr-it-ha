# SPDX-License-Identifier: GPL-3.0-or-later
"""Sensors of electrITHA: those of a supply and those of the GME account."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_MONTH, DOMAIN, NAME
from .coordinator import ElectrIthaCoordinator, ElectrIthaData
from .engine.bands import ITALY, Band
from .engine.calculator import Category
from .engine.gme import ATTRIBUTION
from .engine.market import PUN_PREFIX
from .engine.pun import PunAverages
from .gme_coordinator import GmeCoordinator, GmeData

CURRENCY = "EUR"
PUN_BANDS = ("f0", "f1", "f2", "f3", "f23")


def _number(value: Decimal | None, digits: int) -> float | None:
    """Round a Decimal for display, keeping None."""
    return None if value is None else round(float(value), digits)


def _month_start(data: ElectrIthaData) -> datetime:
    """Return the first instant of the running month, in Italian time."""
    month = data.energy.month
    return datetime(month.year, month.month, 1, tzinfo=ITALY)


def _energy_attributes(data: ElectrIthaData) -> dict[str, Any]:
    """Return the kWh of the running month by band."""
    return {band.value: _number(data.energy.kwh[band], 3) for band in Band}


def _unit_price_attributes(data: ElectrIthaData) -> dict[str, Any]:
    """Return the split of the unit price, in EUR/kWh."""
    price = data.unit_price
    if price is None:
        return {"problems": list(data.problems)}
    return {
        "band": data.band.value,
        "estimated": data.estimated,
        "sale": _number(price.sale, 6),
        "network": _number(price.network, 6),
        "system_charges": _number(price.system_charges, 6),
        "excise": _number(price.excise, 6),
        "vat": _number(price.vat, 6),
    }


def _month_cost_attributes(data: ElectrIthaData) -> dict[str, Any]:
    """Return the split of the month so far, in EUR."""
    bill = data.month_bill
    if bill is None:
        return {"problems": list(data.problems)}
    attributes: dict[str, Any] = {
        "month": data.energy.month.isoformat(),
        "estimated": True,
        "taxable": _number(bill.taxable, 2),
        "vat": _number(bill.vat, 2),
    }
    for category in Category:
        attributes[category.value] = _number(bill.subtotal(category), 2)
    if bill.warnings:
        attributes["warnings"] = list(bill.warnings)
    return attributes


def _closed_month_attributes(data: ElectrIthaData) -> dict[str, Any]:
    """Return the detail of the month that ended."""
    closed = data.closed_month
    if closed is None:
        if data.pending_month is None:
            return {}
        return {"month": data.pending_month.isoformat(), "waiting_for_market": True}
    attributes: dict[str, Any] = {
        "month": closed.energy.month.isoformat(),
        "energy": _number(closed.energy.total, 3),
        "taxable": _number(closed.taxable, 2),
        "vat": _number(closed.vat, 2),
    }
    attributes.update(
        {band.value: _number(closed.energy.kwh[band], 3) for band in Band}
    )
    if closed.problem:
        attributes["problem"] = closed.problem
    return attributes


@dataclass(frozen=True, kw_only=True)
class ElectrIthaSensorDescription(SensorEntityDescription):
    """A sensor and how to read it from the coordinator data."""

    value: Callable[[ElectrIthaData], Any]
    attributes: Callable[[ElectrIthaData], dict[str, Any]] | None = None
    last_reset: Callable[[ElectrIthaData], datetime] | None = None


SENSORS: tuple[ElectrIthaSensorDescription, ...] = (
    ElectrIthaSensorDescription(
        key="unit_price",
        translation_key="unit_price",
        native_unit_of_measurement=f"{CURRENCY}/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda data: (
            None if data.unit_price is None else _number(data.unit_price.total, 6)
        ),
        attributes=_unit_price_attributes,
    ),
    ElectrIthaSensorDescription(
        key="fixed_cost",
        translation_key="fixed_cost",
        native_unit_of_measurement=f"{CURRENCY}/d",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda data: _number(data.fixed_cost_per_day, 4),
    ),
    ElectrIthaSensorDescription(
        key="month_cost",
        translation_key="month_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value=lambda data: (
            None if data.month_bill is None else _number(data.month_bill.total, 2)
        ),
        attributes=_month_cost_attributes,
        last_reset=_month_start,
    ),
    ElectrIthaSensorDescription(
        key="previous_month_cost",
        translation_key="previous_month_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value=lambda data: (
            None if data.closed_month is None else _number(data.closed_month.total, 2)
        ),
        attributes=_closed_month_attributes,
    ),
    ElectrIthaSensorDescription(
        key="month_energy",
        translation_key="month_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value=lambda data: _number(data.energy.total, 3),
        attributes=_energy_attributes,
        last_reset=_month_start,
    ),
    ElectrIthaSensorDescription(
        key="time_band",
        translation_key="time_band",
        device_class=SensorDeviceClass.ENUM,
        options=[band.value for band in Band],
        value=lambda data: data.band.value,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of a supply, or those of the GME account."""
    coordinator = entry.runtime_data
    if isinstance(coordinator, GmeCoordinator):
        async_add_entities(
            GmeSensor(coordinator, band, which)
            for which in ("current", "previous")
            for band in PUN_BANDS
        )
        return
    async_add_entities(
        ElectrIthaSensor(coordinator, description) for description in SENSORS
    )


class ElectrIthaSensor(CoordinatorEntity[ElectrIthaCoordinator], SensorEntity):
    """One value computed for a supply."""

    entity_description: ElectrIthaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ElectrIthaCoordinator,
        description: ElectrIthaSensorDescription,
    ) -> None:
        """Bind the sensor to its supply."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=NAME,
            model=coordinator.contract.supplier,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        """Return the value read from the coordinator data."""
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the detail of the value, when the sensor has one."""
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        """Return the start of the month for the monthly totals."""
        if self.entity_description.last_reset is None:
            return None
        return dt_util.as_utc(self.entity_description.last_reset(self.coordinator.data))


class GmeSensor(CoordinatorEntity[GmeCoordinator], SensorEntity):
    """Average PUN of a band, for the running month or the month before."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION
    _attr_native_unit_of_measurement = f"{CURRENCY}/{UnitOfEnergy.KILO_WATT_HOUR}"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 5

    def __init__(self, coordinator: GmeCoordinator, band: str, which: str) -> None:
        """Describe the sensor; `which` is "current" or "previous"."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._name = f"{PUN_PREFIX}{band}"
        self._which = which
        self._attr_translation_key = f"{self._name}_{which}"
        # The supplies find the last-month twin by this suffix.
        self._attr_unique_id = f"{entry.entry_id}_{self._name}_{which}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=NAME,
            model="PUN Index GME",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _averages(self) -> PunAverages:
        """Return the averages this sensor shows.

        In the first days of a month a band may have no hours yet: until it
        does, the sensor of the running month shows the month before, and its
        `month` attribute says so.
        """
        data: GmeData = self.coordinator.data
        if self._which == "previous":
            return data.previous
        if self._name not in data.current.market_values():
            return data.previous
        return data.current

    @property
    def native_value(self) -> float | None:
        """Return the average in EUR/kWh."""
        value = self._averages.market_values().get(self._name)
        return _number(value, 5)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the month, how many hours it covers and whether it is final."""
        averages = self._averages
        return {
            ATTR_MONTH: f"{averages.month:%Y-%m}",
            "hours": averages.slots,
            "expected_hours": averages.expected,
            "complete": averages.complete,
        }
