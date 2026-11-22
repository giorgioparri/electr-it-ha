# SPDX-License-Identifier: GPL-3.0-or-later
"""The offers of the Portale Offerte, as offers.json keeps them.

A workflow of the repository converts the daily file of the Portale and keeps
the result in offers.json on the market-data branch (tools/update_offers.py).
This reads that file and finds an offer by the code printed on a bill.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Self

from .offers import Status

SCHEMA_VERSION = 1


class CatalogueError(ValueError):
    """The file is not the one the workflow writes."""


def normalize_code(text: str) -> str:
    """The code as typed from a bill: no spaces, upper case."""
    return "".join(text.split()).upper()


@dataclass(frozen=True, slots=True)
class CatalogueOffer:
    """One offer of the file."""

    code: str
    name: str
    supplier: str
    status: Status
    problems: tuple[str, ...]
    first_seen: date
    last_seen: date | None
    contract: Mapping[str, Any] | None

    @property
    def on_sale(self) -> bool:
        """True while the offer is on the Portale."""
        return self.last_seen is None

    def seen_until(self, catalogue: "Catalogue") -> date:
        """The last day the offer was on the Portale, as far as the file knows."""
        return catalogue.updated if self.last_seen is None else self.last_seen

    def contract_json(self) -> str | None:
        """The contract as the contract screen shows it, or None."""
        if self.contract is None:
            return None
        return json.dumps(self.contract, indent=2, ensure_ascii=False)


def _date(value: Any, what: str) -> date:
    """Read an ISO date."""
    if not isinstance(value, str):
        raise CatalogueError(f"{what}: not a date")
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise CatalogueError(f"{what}: not a date: {value!r}") from err


def _offer(code: str, item: Any) -> CatalogueOffer:
    """Read one offer."""
    if not isinstance(item, dict):
        raise CatalogueError(f"{code}: not an object")
    try:
        status = Status(item["status"])
        info = item["info"]
        contract = item["contract"]
        problems = tuple(str(problem) for problem in item["problems"])
        first_seen = _date(item["first_seen"], f"{code}.first_seen")
        raw_last = item["last_seen"]
    except KeyError as err:
        raise CatalogueError(f"{code}: missing {err}") from err
    except (ValueError, TypeError) as err:
        raise CatalogueError(f"{code}: {err}") from err
    if not isinstance(info, dict):
        raise CatalogueError(f"{code}.info: not an object")
    if (contract is None) != (status is Status.UNSUPPORTED):
        raise CatalogueError(
            f"{code}: a contract goes with every status but unsupported"
        )
    if contract is not None and not isinstance(contract, dict):
        raise CatalogueError(f"{code}.contract: not an object")
    return CatalogueOffer(
        code=code,
        name=str(info.get("name") or ""),
        supplier=str(info.get("supplier") or ""),
        status=status,
        problems=problems,
        first_seen=first_seen,
        last_seen=None if raw_last is None else _date(raw_last, f"{code}.last_seen"),
        contract=contract,
    )


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Every offer of the file, by code."""

    updated: date
    offers: Mapping[str, CatalogueOffer]

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read the content of offers.json, or raise CatalogueError."""
        if not isinstance(data, dict):
            raise CatalogueError("not a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise CatalogueError(
                f"unsupported schema {data.get('schema_version')!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        if "updated" not in data:
            raise CatalogueError("missing updated")
        updated = _date(data["updated"], "updated")
        offers = data.get("offers")
        if not isinstance(offers, dict):
            raise CatalogueError("missing offers")
        return cls(updated, {code: _offer(code, item) for code, item in offers.items()})

    def __len__(self) -> int:
        """The number of offers, on sale or not."""
        return len(self.offers)

    def find(self, text: str) -> CatalogueOffer | None:
        """The offer with the code `text`, typed from a bill; None if unknown."""
        code = normalize_code(text)
        if not code:
            return None
        for candidate, offer in self.offers.items():
            if candidate.upper() == code:
                return offer
        return None
