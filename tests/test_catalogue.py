# SPDX-License-Identifier: GPL-3.0-or-later
"""Looking up an offer in offers.json. Run as a script:

    python3 tests/test_catalogue.py

The file is the one tools/update_offers.py writes, built here from the real
offers in tests/fixtures/portale_offerte_20261121.xml, so the reader and the
writer cannot drift apart unnoticed.
"""

import copy
import io
import json
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_offers as u

from engine.catalogue import Catalogue, CatalogueError, normalize_code
from engine.contract import Contract
from engine.offers import Status

FIXTURE = Path(__file__).parent / "fixtures" / "portale_offerte_20261121.xml"
NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
ENEL = "000142ESVFL01XXResECAn0000119094"
TIERS = "000208ESFML04XX000IEDOSTPN261130"
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


# --- The file, as the workflow writes it -----------------------------------------

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


def flat_offer() -> str:
    """A FLAT offer, which gives no contract."""
    block = re.sub(
        r"<COD_OFFERTA>.*?<", f"<COD_OFFERTA>{FLAT}<", BLOCKS["fixed_mono"], count=1
    )
    return block.replace("<TIPO_OFFERTA>01<", "<TIPO_OFFERTA>03<")


def written() -> dict[str, Any]:
    """offers.json after two days: the tiered offer withdrawn on the second."""
    first = u.read_day(daily(*BLOCKS.values(), flat_offer()))
    data = u.merged(None, date(2026, 11, 21), first)
    second = {code: item for code, item in first.items() if code != TIERS}
    data = u.merged(data, date(2026, 11, 22), second)
    # Through the text, as Home Assistant downloads it.
    return json.load(io.StringIO(u.dumps(data)))


FILE = written()


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


def test_lookup() -> None:
    """Offers are found by code, whatever the case, and say what they are."""
    catalogue = Catalogue.from_dict(FILE)
    check("updated", catalogue.updated, date(2026, 11, 22))
    check("offers", len(catalogue), 19)

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

    check("found in upper case", catalogue.find(ENEL.upper()), offer)
    check("found in lower case", catalogue.find(ENEL.lower()), offer)
    check("found with spaces", catalogue.find(f" {ENEL[:6]} {ENEL[6:]}\n"), offer)
    check("unknown code", catalogue.find("000000ESVFL01XX0000000000000000"), None)
    check("empty code", catalogue.find(" "), None)

    withdrawn = catalogue.find(TIERS)
    assert withdrawn is not None
    check("withdrawn: status kept", withdrawn.status, Status.PARTIAL)
    check("withdrawn: problems kept", len(withdrawn.problems) > 0, True)
    check("withdrawn: last seen", withdrawn.last_seen, date(2026, 11, 21))
    check("withdrawn: not on sale", withdrawn.on_sale, False)
    check("withdrawn: seen until", withdrawn.seen_until(catalogue), date(2026, 11, 21))


def test_contract_text() -> None:
    """The text for the contract screen validates as it is; none without one."""
    catalogue = Catalogue.from_dict(FILE)
    offer = catalogue.find(ENEL)
    assert offer is not None
    text = offer.contract_json()
    assert text is not None
    contract = Contract.from_json(text)
    check("contract code", contract.offer_code, ENEL)
    check("indented", text.startswith("{\n  "), True)
    check("plain UTF-8", "\\u" in text, False)

    unsupported = catalogue.find(FLAT)
    assert unsupported is not None
    check("FLAT: unsupported", unsupported.status, Status.UNSUPPORTED)
    check("FLAT: no contract", unsupported.contract_json(), None)
    check("FLAT: a reason", len(unsupported.problems) > 0, True)


def test_bad_files() -> None:
    """A file that is not what the workflow writes is refused as a whole."""
    raises("not an object", CatalogueError, Catalogue.from_dict, [])
    newer = copy.deepcopy(FILE)
    newer["schema_version"] = 2
    raises("another schema", CatalogueError, Catalogue.from_dict, newer)
    for key in ("updated", "offers"):
        broken = copy.deepcopy(FILE)
        del broken[key]
        raises(f"no {key}", CatalogueError, Catalogue.from_dict, broken)
    broken = copy.deepcopy(FILE)
    broken["updated"] = "yesterday"
    raises("a date that is not a date", CatalogueError, Catalogue.from_dict, broken)
    broken = copy.deepcopy(FILE)
    del broken["offers"][ENEL]["status"]
    raises("an offer without status", CatalogueError, Catalogue.from_dict, broken)
    broken = copy.deepcopy(FILE)
    broken["offers"][ENEL]["status"] = "maybe"
    raises("an unknown status", CatalogueError, Catalogue.from_dict, broken)
    broken = copy.deepcopy(FILE)
    broken["offers"][ENEL]["contract"] = None
    raises(
        "a complete offer without contract", CatalogueError, Catalogue.from_dict, broken
    )


def main() -> int:
    """Run every test and report."""
    for test in (test_codes, test_lookup, test_contract_text, test_bad_files):
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
