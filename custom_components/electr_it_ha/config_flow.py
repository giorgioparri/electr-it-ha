# SPDX-License-Identifier: GPL-3.0-or-later
"""Config flow for electrITHA.

The first screen asks what to add, until a GME account exists.

A supply takes three screens, the same for first setup and reconfiguration:
1. the contract, validated on the spot. It is either pasted as JSON, or
   found by the offer code printed on the bill among the offers of the
   Portale Offerte (engine.catalogue): the converted contract then fills the
   same JSON form, to be checked against the bill before going on;
2. the supply (resident, committed power) and the grid import meters. When
   the offer found by code is for residents only, or for non-residents only,
   and the supply is of the other kind, the screen warns once and goes on
   at the next submit: residence belongs to the supply, and the Portale may
   be wrong;
3. where each market value the contract needs comes from: an entity or a
   fixed number. Dispatching, capacity market and CDispD can be left empty,
   for the values Terna and ARERA publish. Skipped when the contract needs
   none. The sensors of the GME account, when there is one, are suggested for
   the PUN.

The GME account takes the credentials GME gives each user, checked by logging
in; the same screen serves reconfiguration and, when GME refuses them, reauth.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CONTRACT,
    CONF_ENTRY_TYPE,
    CONF_MARKET,
    CONF_METERS,
    CONF_POWER_KW,
    CONF_RESIDENT,
    DOMAIN,
    ENTRY_TYPE_GME,
    ENTRY_TYPE_SUPPLY,
    GME_UNIQUE_ID,
)
from .engine.catalogue import CatalogueOffer
from .engine.contract import Contract, ContractError
from .engine.gme import GmeAuthError, GmeError
from .engine.market import PUN_PREFIX, required_market_values
from .engine.offers import Status
from .engine.published import NAMES as PUBLISHED_NAMES
from .gme_client import GmeClient
from .offer_catalogue import OffersUnavailableError, async_get_catalogue

_LOGGER = logging.getLogger(__name__)

CONF_CONTRACT_JSON = "contract_json"
CONF_OFFER_CODE = "offer_code"
ENTITY_SUFFIX = "_entity"
VALUE_SUFFIX = "_value"

DEFAULT_POWER_KW = 3.0
GME_TITLE = "GME"
GME_REGISTRATION_URL = (
    "https://api.mercatoelettrico.org/users/RegistrationForm/RegistrationRequest"
)
GME_PASSWORD_URL = "https://api.mercatoelettrico.org/users/"


def _contract_schema() -> vol.Schema:
    """Return the form of the contract screen."""
    return vol.Schema(
        {
            vol.Required(CONF_CONTRACT_JSON): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            )
        }
    )


def _offer_code_schema() -> vol.Schema:
    """Return the form of the offer code screen."""
    return vol.Schema({vol.Required(CONF_OFFER_CODE): selector.TextSelector()})


def _bullets(lines: tuple[str, ...] | list[str]) -> str:
    """A markdown list, or nothing."""
    return "\n".join(f"- {line}" for line in lines)


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


def _gme_schema() -> vol.Schema:
    """Return the form of the GME account screen."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): selector.TextSelector(),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _is_gme(entry: ConfigEntry) -> bool:
    """Return True for the GME account entry."""
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GME


def _market_form_values(market: dict[str, Any]) -> dict[str, Any]:
    """Turn stored market sources into the values shown in the form."""
    values: dict[str, Any] = {}
    for name, source in market.items():
        suffix = ENTITY_SUFFIX if isinstance(source, str) else VALUE_SUFFIX
        values[f"{name}{suffix}"] = source
    return values


class ElectrIthaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup and reconfiguration of a supply or of the GME account."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing collected."""
        self._contract: Contract | None = None
        self._offer: CatalogueOffer | None = None
        self._offer_seen_until: str = ""
        self._residence_warned = False
        self._data: dict[str, Any] = {}
        self._previous: dict[str, Any] = {}

    def _gme_entry(self) -> ConfigEntry | None:
        """Return the GME account entry, if there is one."""
        return next(filter(_is_gme, self._async_current_entries()), None)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First setup: a supply, or the GME account if there is none yet."""
        if self._gme_entry() is not None:
            return await self.async_step_contract_source(user_input)
        return self.async_show_menu(
            step_id="user", menu_options=["contract_source", "gme_account"]
        )

    async def async_step_contract_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find the contract by its offer code, or paste it."""
        return self.async_show_menu(
            step_id="contract_source", menu_options=["offer_code", "contract"]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfiguration: the same screens, filled with the current values."""
        self._previous = dict(self._get_reconfigure_entry().data)
        if _is_gme(self._get_reconfigure_entry()):
            return await self.async_step_gme_account(user_input)
        return await self.async_step_contract_source(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """GME refused the saved credentials: ask for them again."""
        self._previous = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the GME credentials again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._check_gme(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                _gme_schema(), {CONF_USERNAME: self._previous.get(CONF_USERNAME)}
            ),
            errors=errors,
            description_placeholders={
                "url": GME_REGISTRATION_URL,
                "password_url": GME_PASSWORD_URL,
            },
        )

    async def async_step_gme_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter and check the credentials of the GME account."""
        reconfiguring = self.source == SOURCE_RECONFIGURE
        if not reconfiguring:
            await self.async_set_unique_id(GME_UNIQUE_ID)
            self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._check_gme(user_input)
            if not errors:
                data = {CONF_ENTRY_TYPE: ENTRY_TYPE_GME, **user_input}
                if reconfiguring:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data=data
                    )
                return self.async_create_entry(title=GME_TITLE, data=data)
        suggested = user_input or {CONF_USERNAME: self._previous.get(CONF_USERNAME)}
        return self.async_show_form(
            step_id="gme_account",
            data_schema=self.add_suggested_values_to_schema(_gme_schema(), suggested),
            errors=errors,
            description_placeholders={
                "url": GME_REGISTRATION_URL,
                "password_url": GME_PASSWORD_URL,
            },
        )

    async def _check_gme(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Log in to GME; return the form errors."""
        client = GmeClient(
            async_get_clientsession(self.hass),
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )
        try:
            await client.async_login()
        except GmeAuthError as err:
            _LOGGER.debug("GME login refused: %s", err)
            return {"base": "invalid_auth"}
        except GmeError as err:
            _LOGGER.debug("Could not reach the GME API: %s", err)
            return {"base": "cannot_connect"}
        return {}

    def _gme_suggestions(self, names: tuple[str, ...]) -> dict[str, Any]:
        """Suggest the sensors of the GME account for the PUN values."""
        gme = self._gme_entry()
        if gme is None:
            return {}
        registry = er.async_get(self.hass)
        suggestions: dict[str, Any] = {}
        for name in names:
            if not name.startswith(PUN_PREFIX):
                continue
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{gme.entry_id}_{name}_current"
            )
            if entity_id is not None:
                suggestions[f"{name}{ENTITY_SUFFIX}"] = entity_id
        return suggestions

    async def async_step_offer_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Look up the offer code printed on the bill."""
        errors: dict[str, str] = {}
        details = ""
        if user_input is not None:
            # As typed: find() prefers the code in its own letter case, since
            # the Portale has codes that differ in letter case only.
            code = user_input[CONF_OFFER_CODE]
            try:
                catalogue = await async_get_catalogue(self.hass, code)
            except OffersUnavailableError as err:
                _LOGGER.warning("Cannot look up the offer code: %s", err)
                errors["base"] = "offers_unavailable"
            else:
                offer = catalogue.find(code)
                if offer is None:
                    errors[CONF_OFFER_CODE] = "offer_not_found"
                elif offer.status is Status.UNSUPPORTED:
                    errors[CONF_OFFER_CODE] = "offer_unsupported"
                    details = _bullets(offer.problems)
                else:
                    self._offer = offer
                    self._offer_seen_until = offer.seen_until(catalogue).isoformat()
                    return await self.async_step_offer_contract()

        suggested = user_input
        if suggested is None:
            previous = self._previous.get(CONF_CONTRACT, {}).get("offer_code")
            suggested = {CONF_OFFER_CODE: previous} if previous else None
        return self.async_show_form(
            step_id="offer_code",
            data_schema=self.add_suggested_values_to_schema(
                _offer_code_schema(), suggested
            ),
            errors=errors,
            description_placeholders={"details": details},
        )

    async def async_step_offer_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the contract converted from the offer, then go on."""
        assert self._offer is not None
        offer = self._offer
        errors, details = {}, ""
        if user_input is not None:
            errors, details = self._read_contract(user_input)
            if not errors:
                return await self.async_step_supply()

        suggested = user_input or {CONF_CONTRACT_JSON: offer.contract_json()}
        return self.async_show_form(
            step_id="offer_contract",
            data_schema=self.add_suggested_values_to_schema(
                _contract_schema(), suggested
            ),
            errors=errors,
            description_placeholders={
                "name": offer.name,
                "supplier": offer.supplier,
                "first_seen": offer.first_seen.isoformat(),
                "last_seen": self._offer_seen_until,
                "problems": _bullets(offer.problems),
                "details": details,
            },
        )

    def _read_contract(self, user_input: dict[str, Any]) -> tuple[dict[str, str], str]:
        """Validate the JSON of the form; return the errors and their details."""
        try:
            self._contract = Contract.from_json(user_input[CONF_CONTRACT_JSON])
        except ContractError as err:
            details = "\n".join(f"- {message}" for message in err.errors)
            return {CONF_CONTRACT_JSON: "invalid_contract"}, details
        self._data[CONF_CONTRACT] = self._contract.to_dict()
        return {}, ""

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste and validate the contract."""
        errors: dict[str, str] = {}
        details = ""
        if user_input is not None:
            errors, details = self._read_contract(user_input)
            if not errors:
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
            resident = user_input[CONF_RESIDENT]
            offer = self._offer
            if not user_input[CONF_METERS]:
                errors[CONF_METERS] = "no_meters"
            elif (
                offer is not None
                and not offer.allows(resident=resident)
                and not self._residence_warned
            ):
                # A warning, not a block: the second submit goes on.
                self._residence_warned = True
                errors["base"] = (
                    "offer_non_residents_only" if resident else "offer_residents_only"
                )
            else:
                self._data[CONF_RESIDENT] = resident
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
        """Choose an entity or a fixed number for each market value needed.

        Dispatching, capacity market and CDispD may be left empty: the values
        published by Terna and ARERA are then used.
        """
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
                if entity is None and value is None and name in PUBLISHED_NAMES:
                    continue  # the published values
                if (entity is None) == (value is None):
                    errors[f"{name}{ENTITY_SUFFIX}"] = "entity_or_value"
                else:
                    market[name] = entity if entity is not None else value
            if not errors:
                self._data[CONF_MARKET] = market
                return self._finish()

        suggested = user_input
        if suggested is None:
            previous = self._previous.get(CONF_MARKET, {})
            suggested = {
                **self._gme_suggestions(
                    tuple(name for name in names if name not in previous)
                ),
                **_market_form_values(previous),
            }
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
        self._data[CONF_ENTRY_TYPE] = ENTRY_TYPE_SUPPLY
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data
            )
        title = self._contract.supplier
        if self._contract.offer_name:
            title = f"{title} - {self._contract.offer_name}"
        return self.async_create_entry(title=title, data=self._data)
