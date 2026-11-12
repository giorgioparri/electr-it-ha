# SPDX-License-Identifier: GPL-3.0-or-later
"""Messages of the GME API (https://api.mercatoelettrico.org).

Only what goes into a request and what comes out of a response: sending them
is up to the caller, which also owns the credentials. Each user registers
with GME and uses their own login, as GME's terms of use require.

A data response carries a .zip file in base64 holding one .json file: for
ME_ZonalPrices, a list of rows with FlowDate (yyyyMMdd), Hour (1-25),
Period (1-100), Market, Zone, Price (EUR/MWh) and Notes. The PUN Index GME is
the row whose Zone is "PUN".

Pure Python: no Home Assistant imports are allowed in this package.
"""

import base64
import binascii
import io
import json
import zipfile
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .pun import PunError, slot_start

API_BASE = "https://api.mercatoelettrico.org/request"
AUTH_URL = f"{API_BASE}/api/v1/Auth"
DATA_URL = f"{API_BASE}/api/v1/RequestData"
QUOTAS_URL = f"{API_BASE}/api/v1/GetMyQuotas"
ATTRIBUTION = "Source: Gestore dei Mercati Energetici S.p.A."

_PUN_ZONE = "PUN"
_GRANULARITY = {60: "PT60", 15: "PT15"}


class GmeError(Exception):
    """GME refused a request, or its answer cannot be read."""


class GmeAuthError(GmeError):
    """The login was refused."""


def auth_body(login: str, password: str) -> dict[str, str]:
    """Return the body of the authentication request."""
    return {"Login": login, "Password": password}


def token_from(answer: Any) -> str:
    """Return the JWT of a successful authentication."""
    if not isinstance(answer, Mapping):
        raise GmeAuthError("the authentication answer is not an object")
    token = answer.get("token")
    if answer.get("Success") is not True or not token:
        reason = answer.get("Reason") or "no reason given"
        raise GmeAuthError(f"login refused: {reason}")
    return str(token)


def _yyyymmdd(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def pun_request(start: date, end: date, minutes: int = 60) -> dict[str, Any]:
    """Return the body asking for the PUN from `start` to `end`, both included."""
    if end < start:
        raise GmeError(f"interval ends ({end}) before it starts ({start})")
    if minutes not in _GRANULARITY:
        raise GmeError(f"slots of {minutes} minutes are not supported")
    return {
        "Platform": "PublicMarketResults",
        "Segment": "MGP",
        "DataName": "ME_ZonalPrices",
        "IntervalStart": _yyyymmdd(start),
        "IntervalEnd": _yyyymmdd(end),
        "Attributes": {"GranularityType": _GRANULARITY[minutes]},
    }


def _rows(answer: Any) -> list[Any]:
    """Unpack the list of rows carried by a data answer."""
    if not isinstance(answer, Mapping):
        raise GmeError("the data answer is not an object")
    content = answer.get("ContentResponse")
    if not content:
        reason = answer.get("ResultRequest") or "no reason given"
        raise GmeError(f"request failed: {reason}")
    try:
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(content, validate=True)))
        names = [name for name in archive.namelist() if name.endswith(".json")]
        if len(names) != 1:
            raise GmeError(f"expected one .json file, found {len(names)}")
        rows = json.loads(archive.read(names[0]), parse_float=Decimal)
    except (binascii.Error, zipfile.BadZipFile, ValueError) as err:
        raise GmeError(f"unreadable answer: {err}") from err
    if not isinstance(rows, list):
        raise GmeError("the answer does not hold a list of rows")
    return rows


def _price(row: Mapping[str, Any]) -> Decimal | None:
    value = row["Price"]
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise GmeError(f"price '{value}' is not a number") from None


def pun_prices(answer: Any, minutes: int = 60) -> list[tuple[datetime, Decimal]]:
    """Return the PUN prices in a data answer, each with the start of its slot.

    Rows of other zones are skipped, and so are rows without a price (an
    auction cancelled). Prices stay in EUR/MWh, as GME publishes them.
    """
    prices: list[tuple[datetime, Decimal]] = []
    for row in _rows(answer):
        if not isinstance(row, Mapping) or row.get("Zone") != _PUN_ZONE:
            continue
        try:
            price = _price(row)
            if price is None:
                continue
            flow = str(row["FlowDate"])
            day = date(int(flow[:4]), int(flow[4:6]), int(flow[6:8]))
            index = int(row["Hour"] if minutes == 60 else row["Period"])
            prices.append((slot_start(day, index, minutes), price))
        except (KeyError, TypeError, ValueError, PunError) as err:
            raise GmeError(f"unreadable row {dict(row)}: {err}") from err
    return prices
