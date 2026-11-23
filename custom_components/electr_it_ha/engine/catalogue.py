# SPDX-License-Identifier: GPL-3.0-or-later
"""The offers of the Portale Offerte, as the offers/ folder keeps them.

A workflow of the repository converts the daily file of the Portale and keeps
the result in the offers/ folder of the market-data branch
(tools/update_offers.py). The offers are split into files by the hash of
their code, so that a lookup downloads one small file and a daily commit
touches only the files that changed:

- offers/index.json: when the folder was updated, from which daily file, and
  how a code maps to its file;
- offers/<hh>.json: the offers whose code, as typed from a bill, has an
  SHA-256 starting with the hex digits <hh>.

This reads them and finds an offer by the code printed on a bill.

Pure Python: no Home Assistant imports are allowed in this package.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Self

from .offers import Status

SCHEMA_VERSION = 2
INDEX_FILE = "index.json"
HASH = "sha256"
# How many hex digits of the hash name a file: 2 gives 256 files.
MAX_DIGITS = 4


class CatalogueError(ValueError):
    """The file is not the one the workflow writes."""


def normalize_code(text: str) -> str:
    """The code as typed from a bill: no spaces, upper case."""
    return "".join(text.split()).upper()


def shard_key(text: str, digits: int) -> str:
    """The first `digits` hex digits of the hash of the code typed as `text`.

    The hash is taken on the normalized code, so the spellings of a code that
    differ in spaces or letter case land in the same file.
    """
    return hashlib.sha256(normalize_code(text).encode("utf-8")).hexdigest()[:digits]


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


def _schema(data: Any, what: str) -> dict[str, Any]:
    """Check that `data` is an object of the current schema."""
    if not isinstance(data, dict):
        raise CatalogueError(f"{what}: not a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise CatalogueError(
            f"{what}: unsupported schema {data.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    return data


def _count(data: dict[str, Any], key: str) -> int:
    """Read a count of the index."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogueError(f"{INDEX_FILE}: missing or bad {key}")
    return value


@dataclass(frozen=True, slots=True)
class CatalogueIndex:
    """index.json: the state of the folder, and the file that holds a code."""

    updated: date
    file: str
    digits: int
    offers: int
    on_sale: int

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        """Read the content of index.json, or raise CatalogueError."""
        data = _schema(data, INDEX_FILE)
        try:
            updated = _date(data["updated"], "updated")
            file = data["file"]
            shards = data["shards"]
        except KeyError as err:
            raise CatalogueError(f"{INDEX_FILE}: missing {err}") from err
        if not isinstance(file, str):
            raise CatalogueError(f"{INDEX_FILE}: file is not a name")
        if not isinstance(shards, dict) or shards.get("hash") != HASH:
            raise CatalogueError(f"{INDEX_FILE}: shards are not split by {HASH}")
        digits = shards.get("digits")
        if (
            isinstance(digits, bool)
            or not isinstance(digits, int)
            or not 1 <= digits <= MAX_DIGITS
        ):
            raise CatalogueError(f"{INDEX_FILE}: bad digits {digits!r}")
        return cls(
            updated=updated,
            file=file,
            digits=digits,
            offers=_count(data, "offers"),
            on_sale=_count(data, "on_sale"),
        )

    def shard_file(self, text: str) -> str | None:
        """The name of the file holding the code typed as `text`; None if empty."""
        if not normalize_code(text):
            return None
        return f"{shard_key(text, self.digits)}.json"


@dataclass(frozen=True, slots=True)
class Catalogue:
    """The offers of one file of the folder, by code."""

    updated: date
    offers: Mapping[str, CatalogueOffer]

    @classmethod
    def from_shard(cls, index: CatalogueIndex, name: str, data: Any) -> Self:
        """Read the file `name` of the folder, or raise CatalogueError.

        Every offer must belong to that file: an index and a file written
        with different layouts are refused rather than half read.
        """
        data = _schema(data, name)
        offers = data.get("offers")
        if not isinstance(offers, dict):
            raise CatalogueError(f"{name}: missing offers")
        for code in offers:
            if index.shard_file(code) != name:
                raise CatalogueError(f"{name}: {code} belongs to another file")
        return cls(
            index.updated, {code: _offer(code, item) for code, item in offers.items()}
        )

    def __len__(self) -> int:
        """The number of offers, on sale or not."""
        return len(self.offers)

    def find(self, text: str) -> CatalogueOffer | None:
        """The offer with the code `text`, typed from a bill; None if unknown.

        The Portale has republished offers with the letter case of their code
        changed, so a code can match more than one offer. The code as typed
        wins; failing that, the offer on sale; failing that, the one seen last.
        """
        typed = "".join(text.split())
        if not typed:
            return None
        if (exact := self.offers.get(typed)) is not None:
            return exact
        code = typed.upper()
        matches = [
            offer
            for candidate, offer in self.offers.items()
            if candidate.upper() == code
        ]
        if not matches:
            return None
        return max(
            matches, key=lambda offer: (offer.on_sale, offer.last_seen or date.min)
        )
