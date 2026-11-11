# SPDX-License-Identifier: GPL-3.0-or-later
"""Config flow for electrITHA.

Three screens, the same for first setup and for reconfiguration:
1. the contract, pasted as JSON and validated on the spot;
2. the supply (resident, committed power) and the grid import meters;
3. where each market value the contract needs comes from: an entity or a
   fixed number. Skipped when the contract needs none.
"""

import json
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.helpers import selector

from .const import (
    CONF_CONTRACT,
    CONF_MARKET,
    CONF_METERS,
    CONF_POWER_KW,
    CONF_RESIDENT,
    DOMAIN,
)
from .engine.contract import Contract, ContractError
from .engine.market import required_market_values

CONF_CONTRACT_JSON = "contract_json"
ENTITY_SUFFIX = "_entity"
VALUE_SUFFIX = "_value"

DEFAULT_POWER_KW = 3.0


def _contract_schema() -> vol.Schema:
    """Return the form of the contract screen."""
    return vol.Schema(
        {
            vol.Required(CONF_CONTRACT_JSON): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            )
        }
    )


def _supply_schema() -> vol.Schema:
    """Return the form of the supply screen."""
    return vol.Schema(
        {
            vol.Required(CONF_RESIDENT, default=True): selector.BooleanSelector(),
            vol.Required(
                CONF_POWER_KW, default=DEFAULT_POWER_KW
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5,
                    max=30,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="kW",
                )
            ),
            vol.Required(CONF_METERS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class=SensorDeviceClass.ENERGY,
                    multiple=True,
                )
            ),
        }
    )


def _market_schema(names: tuple[str, ...]) -> vol.Schema:
    """Return the form of the market screen: an entity or a number per value."""
    fields: dict[vol.Marker, Any] = {}
    for name in names:
        fields[vol.Optional(f"{name}{ENTITY_SUFFIX}")] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])
        )
        fields[vol.Optional(f"{name}{VALUE_SUFFIX}")] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-1,
                max=5,
                step="any",
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="€/kWh",
            )
        )
    return vol.Schema(fields)


def _market_form_values(market: dict[str, Any]) -> dict[str, Any]:
    """Turn stored market sources into the values shown in the form."""
    values: dict[str, Any] = {}
    for name, source in market.items():
        suffix = ENTITY_SUFFIX if isinstance(source, str) else VALUE_SUFFIX
        values[f"{name}{suffix}"] = source
    return values


class ElectrIthaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup and reconfiguration of a supply."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing collected."""
        self._contract: Contract | None = None
        self._data: dict[str, Any] = {}
        self._previous: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First setup: start from the contract screen."""
        return await self.async_step_contract(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfiguration: the same screens, filled with the current values."""
        self._previous = dict(self._get_reconfigure_entry().data)
        return await self.async_step_contract(user_input)

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste and validate the contract."""
        errors: dict[str, str] = {}
        details = ""
        if user_input is not None:
            try:
                self._contract = Contract.from_json(user_input[CONF_CONTRACT_JSON])
            except ContractError as err:
                errors[CONF_CONTRACT_JSON] = "invalid_contract"
                details = "\n".join(f"- {message}" for message in err.errors)
            else:
                self._data[CONF_CONTRACT] = self._contract.to_dict()
                return await self.async_step_supply()

        suggested = user_input
        if suggested is None and CONF_CONTRACT in self._previous:
            text = json.dumps(self._previous[CONF_CONTRACT], indent=2)
            suggested = {CONF_CONTRACT_JSON: text}
        return self.async_show_form(
            step_id="contract",
            data_schema=self.add_suggested_values_to_schema(
                _contract_schema(), suggested
            ),
            errors=errors,
            description_placeholders={"details": details},
        )

    async def async_step_supply(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Describe the supply and choose the grid import meters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_METERS]:
                errors[CONF_METERS] = "no_meters"
            else:
                self._data[CONF_RESIDENT] = user_input[CONF_RESIDENT]
                self._data[CONF_POWER_KW] = user_input[CONF_POWER_KW]
                self._data[CONF_METERS] = list(user_input[CONF_METERS])
                return await self.async_step_market()

        return self.async_show_form(
            step_id="supply",
            data_schema=self.add_suggested_values_to_schema(
                _supply_schema(), user_input or self._previous
            ),
            errors=errors,
        )

    async def async_step_market(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose an entity or a fixed number for each market value needed."""
        assert self._contract is not None
        names = required_market_values(self._contract)
        if not names:
            self._data[CONF_MARKET] = {}
            return self._finish()

        errors: dict[str, str] = {}
        if user_input is not None:
            market: dict[str, Any] = {}
            for name in names:
                entity = user_input.get(f"{name}{ENTITY_SUFFIX}")
                value = user_input.get(f"{name}{VALUE_SUFFIX}")
                if (entity is None) == (value is None):
                    errors[f"{name}{ENTITY_SUFFIX}"] = "entity_or_value"
                else:
                    market[name] = entity if entity is not None else value
            if not errors:
                self._data[CONF_MARKET] = market
                return self._finish()

        suggested = user_input
        if suggested is None:
            suggested = _market_form_values(self._previous.get(CONF_MARKET, {}))
        return self.async_show_form(
            step_id="market",
            data_schema=self.add_suggested_values_to_schema(
                _market_schema(names), suggested
            ),
            errors=errors,
        )

    def _finish(self) -> ConfigFlowResult:
        """Create the entry, or update it when reconfiguring."""
        assert self._contract is not None
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data
            )
        title = self._contract.supplier
        if self._contract.offer_name:
            title = f"{title} - {self._contract.offer_name}"
        return self.async_create_entry(title=title, data=self._data)
