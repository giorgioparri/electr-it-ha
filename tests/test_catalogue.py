# SPDX-License-Identifier: GPL-3.0-or-later
"""Looking up an offer in the offers/ folder. Run as a script:

    python3 tests/test_catalogue.py

The files are the ones tools/update_offers.py writes, built here from the real
offers in tests/fixtures/portale_offerte_20261121.xml, so the reader and the
writer cannot drift apart unnoticed.
"""

import copy
import json
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_offers as u

from engine.catalogue import (
    INDEX_FILE,
    Catalogue,
    CatalogueError,
    CatalogueIndex,
    normalize_code,
    shard_key,
)
from engine.contract import Contract
from engine.offers import Status

FIXTURE = Path(__file__).parent / "fixtures" / "portale_offerte_20261121.xml"
NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
ENEL = "000142ESVFL01XXResECAn0000119094"
# The same offer under its code in upper case: the Portale has republished
# offers with the letter case of the code changed. It sorts before ENEL.
ENEL_UPPER = ENEL.upper()
TIERS = "000208ESFML04XX000IEDOSTPN261130"
TIERS_LOWER = TIERS[:15] + TIERS[15:].lower()
FLAT = "020536ESFML01XXOTT2026FSEPADFLAT"
_failures: list[str] = []


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual!r}, expected {expected!r}")
    if not ok:
        _failures.append(what)


def raises(
    what: str, error: type[Exception], call: Callable[..., object], *args: object
) -> None:
    """Check that a call raises `error`."""
    try:
        call(*args)
    except error as err:
        print(f"PASS  {what}: {err}")
    else:
        print(f"FAIL  {what}: no {error.__name__} raised")
        _failures.append(what)


# --- The files, as the workflow writes them --------------------------------------

BLOCKS: dict[str, str] = dict(
    re.findall(
        r"<!-- (\w+) -->\s*(<offerta>.*?</offerta>)",
        FIXTURE.read_text(encoding="utf-8"),
        re.S,
    )
)


def daily(*offers: str) -> bytes:
    """A daily file of the Portale holding the given offers."""
    body = "\n".join(offers)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<ListaOfferteMercatoLibero xmlns="{NAMESPACE}">\n{body}\n'
        "</ListaOfferteMercatoLibero>\n"
    ).encode()


def recoded(block: str, code: str) -> str:
    """An offer of the fixture under another code."""
    return re.sub(r"<COD_OFFERTA>.*?<", f"<COD_OFFERTA>{code}<", block, count=1)


def flat_offer() -> str:
    """A FLAT offer, which gives no contract."""
    block = recoded(BLOCKS["fixed_mono"], FLAT)
    return block.replace("<TIPO_OFFERTA>01<", "<TIPO_OFFERTA>03<")


def enel_block() -> str:
    """The block of the fixture that holds ENEL."""
    return next(block for block in BLOCKS.values() if f">{ENEL}<" in block)


def written() -> dict[str, Any]:
    """The files after two days, as text read back into JSON, by name.

    On the first day ENEL is also on sale under ENEL_UPPER; on the second the
    tiered offer and ENEL_UPPER are withdrawn.
    """
    first = u.read_day(
        daily(*BLOCKS.values(), flat_offer(), recoded(enel_block(), ENEL_UPPER))
    )
    data = u.merged(None, date(2026, 11, 21), first)
    second = {
        code: item for code, item in first.items() if code not in (TIERS, ENEL_UPPER)
    }
    data = u.merged(data, date(2026, 11, 22), second)
    # Through the text, as Home Assistant downloads it.
    return {name: json.loads(text) for name, text in u.files(data).items()}


FILES = written()
INDEX = CatalogueIndex.from_dict(FILES[INDEX_FILE])


def shard_of(code: str) -> tuple[str, dict[str, Any]]:
    """The name and the content of the file that holds `code`."""
    name = INDEX.shard_file(code)
    assert name is not None
    return name, FILES[name]


def catalogue_of(code: str) -> Catalogue:
    """The offers of the file that holds `code`."""
    name, data = shard_of(code)
    return Catalogue.from_shard(INDEX, name, data)


# --- Tests ------------------------------------------------------------------------


def test_codes() -> None:
    """A code is typed from a bill: spaces and letter case do not matter."""
    check("as printed", normalize_code(ENEL), ENEL.upper())
    check(
        "spaces and new lines",
        normalize_code(" 000142 ESVFL01XX\nResECAn 0000119094 "),
        ENEL.upper(),
    )
    check("lower case", normalize_code(ENEL.lower()), ENEL.upper())
    check("nothing", normalize_code("   "), "")


def test_shards() -> None:
    """The file of a code comes from the hash of the code as typed from a bill."""
    # Fixed values: a change here moves every offer to another file.
    check("hash of ENEL", shard_key(ENEL, 2), "5c")
    check("hash of TIERS", shard_key(TIERS, 2), "c4")
    check("more digits", shard_key(ENEL, 4), "5c03")
    check("case and spaces do not move a code", shard_key(f" {ENEL.lower()} ", 2), "5c")
    check("file of ENEL", INDEX.shard_file(ENEL), "5c.json")
    check("file of an empty code", INDEX.shard_file(" \n"), None)

    check("index: updated", INDEX.updated, date(2026, 11, 22))
    check("index: daily file", INDEX.file, "PO_Offerte_E_MLIBERO_20261122.xml")
    check("index: digits", INDEX.digits, 2)
    check("index: offers", INDEX.offers, 20)
    check("index: on sale", INDEX.on_sale, 18)

    names = set(FILES) - {INDEX_FILE}
    check(
        "only files that hold offers",
        all(FILES[name]["offers"] for name in names),
        True,
    )
    check(
        "file names",
        all(re.fullmatch(r"[0-9a-f]{2}\.json", name) for name in names),
        True,
    )
    every = [(name, code) for name in sorted(names) for code in FILES[name]["offers"]]
    check("every offer once", len(every), INDEX.offers)
    check(
        "every offer in its own file",
        all(INDEX.shard_file(code) == name for name, code in every),
        True,
    )
    check(
        "offers sorted by code",
        all(list(FILES[n]["offers"]) == sorted(FILES[n]["offers"]) for n in names),
        True,
    )


def test_lookup() -> None:
    """Offers are found by code, whatever the case, and say what they are."""
    catalogue = catalogue_of(ENEL)
    check("updated", catalogue.updated, date(2026, 11, 22))
    check("same file for both spellings", INDEX.shard_file(ENEL_UPPER), "5c.json")

    offer = catalogue.find(ENEL)
    check("found as printed", offer is not None, True)
    assert offer is not None
    check("code as the Portale has it", offer.code, ENEL)
    check("name", offer.name, "ENEL FLEX LUCE")
    check("supplier", offer.supplier, "enel.it")
    check("status", offer.status, Status.COMPLETE)
    check("no problems", offer.problems, ())
    check("first seen", offer.first_seen, date(2026, 11, 21))
    check("last seen while on sale", offer.last_seen, None)
    check("on sale", offer.on_sale, True)
    check("seen until the update", offer.seen_until(catalogue), date(2026, 11, 22))

    check("found in lower case", catalogue.find(ENEL.lower()), offer)
    check("found with spaces", catalogue.find(f" {ENEL[:6]} {ENEL[6:]}\n"), offer)
    check("unknown code", catalogue.find("000000ESVFL01XX0000000000000000"), None)
    check("empty code", catalogue.find(" "), None)

    withdrawn = catalogue_of(TIERS).find(TIERS)
    assert withdrawn is not None
    check("withdrawn: status kept", withdrawn.status, Status.PARTIAL)
    check("withdrawn: problems kept", len(withdrawn.problems) > 0, True)
    check("withdrawn: last seen", withdrawn.last_seen, date(2026, 11, 21))
    check("withdrawn: not on sale", withdrawn.on_sale, False)
    check("withdrawn: seen until", withdrawn.seen_until(catalogue), date(2026, 11, 21))


def test_same_code_other_case() -> None:
    """Codes that differ in letter case only: the best match wins, not the first."""
    catalogue = catalogue_of(ENEL)
    exact = catalogue.find(ENEL_UPPER)
    assert exact is not None
    check("typed exactly: that offer", exact.code, ENEL_UPPER)
    check("typed exactly: even if withdrawn", exact.on_sale, False)
    lower = catalogue.find(ENEL.lower())
    assert lower is not None
    check("no exact match: the one on sale", lower.code, ENEL)

    # Both withdrawn: the one seen last. TIERS_LOWER sorts after TIERS.
    name, data = shard_of(TIERS)
    data = copy.deepcopy(data)
    older = data["offers"][TIERS]
    older["first_seen"] = older["last_seen"] = "2026-11-20"
    data["offers"][TIERS_LOWER] = {
        **copy.deepcopy(older),
        "first_seen": "2026-11-21",
        "last_seen": "2026-11-21",
    }
    both = Catalogue.from_shard(INDEX, name, data)
    found = both.find(f" {TIERS.lower()} ")
    assert found is not None
    check("no exact match, none on sale: seen last", found.code, TIERS_LOWER)
    exact = both.find(TIERS)
    assert exact is not None
    check("typed exactly: the older one", exact.code, TIERS)


def test_residence() -> None:
    """Who can take an offer, as the Portale states it, for the supply screen."""
    offer = catalogue_of(ENEL).find(ENEL)
    assert offer is not None
    check("not stated: all", offer.residence, "all")
    check("not stated: residents allowed", offer.allows(resident=True), True)
    check("not stated: non-residents allowed", offer.allows(resident=False), True)

    residents = "000000ESFML01XX0RESIDENTI000001"
    others = "000000ESFML01XX0NONRESIDENTI001"
    block = BLOCKS["fixed_mono"]
    offers = u.read_day(
        daily(
            recoded(block.replace(">03<", ">01<"), residents),
            recoded(block.replace(">03<", ">02<"), others),
        )
    )
    data = u.merged(None, date(2026, 11, 22), offers)
    files = {name: json.loads(text) for name, text in u.files(data).items()}
    index = CatalogueIndex.from_dict(files[INDEX_FILE])

    def found(code: str) -> Any:
        name = index.shard_file(code)
        assert name is not None
        return Catalogue.from_shard(index, name, files[name]).find(code)

    only = found(residents)
    check("residents only", only.residence, "resident")
    check("residents only: residents allowed", only.allows(resident=True), True)
    check("residents only: others refused", only.allows(resident=False), False)
    not_only = found(others)
    check("non-residents only", not_only.residence, "non_resident")
    check(
        "non-residents only: residents refused", not_only.allows(resident=True), False
    )
    check("non-residents only: others allowed", not_only.allows(resident=False), True)

    # A file written before the key existed, or with a value from a later
    # version: no warning rather than a wrong one.
    name, data = shard_of(ENEL)
    for value in (None, "students"):
        changed = copy.deepcopy(data)
        if value is None:
            changed["offers"][ENEL]["info"].pop("residence", None)
        else:
            changed["offers"][ENEL]["info"]["residence"] = value
        offer = Catalogue.from_shard(INDEX, name, changed).find(ENEL)
        assert offer is not None
        check(f"residence {value!r}: all", offer.residence, "all")


def test_contract_text() -> None:
    """The text for the contract screen validates as it is; none without one."""
    offer = catalogue_of(ENEL).find(ENEL)
    assert offer is not None
    text = offer.contract_json()
    assert text is not None
    contract = Contract.from_json(text)
    check("contract code", contract.offer_code, ENEL)
    check("indented", text.startswith("{\n  "), True)
    check("plain UTF-8", "\\u" in text, False)

    unsupported = catalogue_of(FLAT).find(FLAT)
    assert unsupported is not None
    check("FLAT: unsupported", unsupported.status, Status.UNSUPPORTED)
    check("FLAT: no contract", unsupported.contract_json(), None)
    check("FLAT: a reason", len(unsupported.problems) > 0, True)


def test_bad_index() -> None:
    """An index that is not what the workflow writes is refused."""
    index = FILES[INDEX_FILE]
    raises("not an object", CatalogueError, CatalogueIndex.from_dict, [])
    older = copy.deepcopy(index)
    older["schema_version"] = 1
    raises("the schema of offers.json", CatalogueError, CatalogueIndex.from_dict, older)
    for key in ("updated", "file", "shards", "offers", "on_sale"):
        broken = copy.deepcopy(index)
        del broken[key]
        raises(f"no {key}", CatalogueError, CatalogueIndex.from_dict, broken)
    broken = copy.deepcopy(index)
    broken["updated"] = "yesterday"
    raises(
        "a date that is not a date", CatalogueError, CatalogueIndex.from_dict, broken
    )
    broken = copy.deepcopy(index)
    broken["shards"]["hash"] = "md5"
    raises("another hash", CatalogueError, CatalogueIndex.from_dict, broken)
    for digits in (0, 5, "2", True):
        broken = copy.deepcopy(index)
        broken["shards"]["digits"] = digits
        raises(f"digits {digits!r}", CatalogueError, CatalogueIndex.from_dict, broken)


def test_bad_shards() -> None:
    """A file of offers that is not what the workflow writes is refused whole."""
    name, data = shard_of(ENEL)
    raises("not an object", CatalogueError, Catalogue.from_shard, INDEX, name, [])
    for schema in (1, None):
        broken = copy.deepcopy(data)
        broken["schema_version"] = schema
        raises(
            f"schema {schema!r}",
            CatalogueError,
            Catalogue.from_shard,
            INDEX,
            name,
            broken,
        )
    broken = copy.deepcopy(data)
    del broken["offers"]
    raises("no offers", CatalogueError, Catalogue.from_shard, INDEX, name, broken)
    broken = copy.deepcopy(data)
    del broken["offers"][ENEL]["status"]
    raises(
        "an offer without status",
        CatalogueError,
        Catalogue.from_shard,
        INDEX,
        name,
        broken,
    )
    broken = copy.deepcopy(data)
    broken["offers"][ENEL]["status"] = "maybe"
    raises(
        "an unknown status", CatalogueError, Catalogue.from_shard, INDEX, name, broken
    )
    broken = copy.deepcopy(data)
    broken["offers"][ENEL]["contract"] = None
    raises(
        "a complete offer without contract",
        CatalogueError,
        Catalogue.from_shard,
        INDEX,
        name,
        broken,
    )
    # An offer in the wrong file: an index and a file from different layouts.
    other, other_data = shard_of(TIERS)
    raises(
        "an offer of another file",
        CatalogueError,
        Catalogue.from_shard,
        INDEX,
        name,
        other_data,
    )
    check(
        "the right file is read",
        len(Catalogue.from_shard(INDEX, other, other_data)) > 0,
        True,
    )


def main() -> int:
    """Run every test and report."""
    for test in (
        test_codes,
        test_shards,
        test_lookup,
        test_same_code_other_case,
        test_residence,
        test_contract_text,
        test_bad_index,
        test_bad_shards,
    ):
        try:
            test()
        except Exception as err:  # a crash is a failed test, not the end of the run
            print(f"FAIL  {test.__name__}: {type(err).__name__}: {err}")
            _failures.append(test.__name__)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
