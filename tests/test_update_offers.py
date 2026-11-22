# SPDX-License-Identifier: GPL-3.0-or-later
"""The updater of offers.json. Run as a script:

    python3 tests/test_update_offers.py

Nothing is downloaded: the daily files of the Portale are built here from the
real offers in tests/fixtures/portale_offerte_20261121.xml, and served by a
fake download keyed by URL.
"""

import contextlib
import io
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_offers as u

from engine.contract import Contract

FIXTURE = Path(__file__).parent / "fixtures" / "portale_offerte_20261121.xml"
NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
_failures: list[str] = []


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual!r}, expected {expected!r}")
    if not ok:
        _failures.append(what)


# --- Daily files ----------------------------------------------------------------

BLOCKS: dict[str, str] = dict(
    re.findall(
        r"<!-- (\w+) -->\s*(<offerta>.*?</offerta>)",
        FIXTURE.read_text(encoding="utf-8"),
        re.S,
    )
)
AJO = "030205ESVFL10XXXLEGGERXULTRA1026"
TIERS = "000208ESFML04XX000IEDOSTPN261130"
NEW = "020536ESFML01XXOTT2026FSEPAD1226"
BUSINESS = "020536ESFML01XXOTT2026FSEPAD9999"
OLD = "020536ESFML01XXOTT2026FSEPAD0926"


def renamed(case: str, code: str) -> str:
    """The offer of `case` under another code."""
    return re.sub(r"<COD_OFFERTA>.*?<", f"<COD_OFFERTA>{code}<", BLOCKS[case], count=1)


def daily(*offers: str) -> bytes:
    """A daily file of the Portale holding the given offers."""
    body = "\n".join(offers)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<ListaOfferteMercatoLibero xmlns="{NAMESPACE}">\n{body}\n'
        "</ListaOfferteMercatoLibero>\n"
    ).encode()


DAY_1 = daily(*BLOCKS.values())
# The next day: the tiered offer is withdrawn, Ajo raises its spread, a new
# offer appears, and an offer for other uses is ignored.
DAY_2 = daily(
    *(
        block.replace("<PREZZO>0.050</PREZZO>", "<PREZZO>0.060</PREZZO>")
        for case, block in BLOCKS.items()
        if case != "tiers"
    ),
    renamed("fixed_mono", NEW),
    renamed("fixed_mono", BUSINESS).replace("<TIPO_CLIENTE>01<", "<TIPO_CLIENTE>02<"),
)
# An older day: the tiered offer at another price, and an offer gone since.
DAY_0 = daily(
    BLOCKS["tiers"].replace("0.179300", "0.169300"),
    renamed("fixed_mono", OLD),
)
TRUNCATED = daily(BLOCKS["fixed_mono"], BLOCKS["green"])


class Portale:
    """A fake download: the files it holds, by URL; 404 for the others."""

    def __init__(self, files: dict[date, bytes]) -> None:
        """Serve `files`, one per day."""
        self.files = {u.file_url(day): data for day, data in files.items()}
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        """Return the file at `url`, or raise as a 404 does."""
        self.asked.append(url)
        if url not in self.files:
            raise u.NotFoundError(f"404 for {url}")
        return self.files[url]


def run(output: Path, portale: Portale, *args: str) -> tuple[int, str, str]:
    """Run the updater; return the exit status, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = u.main(["--output", str(output), *args], get=portale)
    return status, out.getvalue(), err.getvalue()


def load(path: Path) -> dict:
    """Read offers.json."""
    return json.loads(path.read_text(encoding="utf-8"))


# --- Tests ------------------------------------------------------------------------


def test_url() -> None:
    """The daily files live in one folder per month."""
    check(
        "URL of a daily file",
        u.file_url(date(2026, 11, 21)),
        "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/"
        "offerteML/2026_11/PO_Offerte_E_MLIBERO_20261121.xml",
    )


def test_daily(folder: Path) -> None:
    """Day after day: added, changed and withdrawn offers."""
    output = folder / "offers.json"
    portale = Portale({date(2026, 11, 21): DAY_1, date(2026, 11, 22): DAY_2})

    status, out, err = run(output, portale, "--today", "2026-11-21")
    check("first day: status", status, 0)
    data = load(output)
    check(
        "first day: header",
        {k: data[k] for k in data if k != "offers"},
        {
            "schema_version": 1,
            "source": "Portale Offerte (ARERA / Acquirente Unico), open data",
            "updated": "2026-11-21",
            "file": "PO_Offerte_E_MLIBERO_20261121.xml",
        },
    )
    check("first day: domestic offers", len(data["offers"]), 18)
    check("first day: one line per offer", out.count("added "), 18)
    entry = data["offers"][AJO]
    check("first day: first seen", entry["first_seen"], "2026-11-21")
    check("first day: on sale", entry["last_seen"], None)
    check("first day: converter", entry["converter"], u.CONVERTER)
    check("first day: status of the offer", entry["status"], "complete")
    check("first day: supplier", entry["info"]["supplier"], "ajoenergia.it")
    check("first day: power limit", entry["info"]["power_max_kw"], 6.0)
    check("first day: valid to", entry["info"]["valid_to"], "2026-11-22T23:59:59")
    check("first day: spread", entry["contract"]["energy_price"]["f1"], 0.05)

    status, out, err = run(output, portale, "--today", "2026-11-22")
    check("second day: status", status, 0)
    data = load(output)
    check("second day: updated", data["updated"], "2026-11-22")
    check("second day: file", data["file"], "PO_Offerte_E_MLIBERO_20261122.xml")
    check("second day: offers kept", len(data["offers"]), 19)
    check("second day: other uses left out", BUSINESS in data["offers"], False)
    check("withdrawn: last seen", data["offers"][TIERS]["last_seen"], "2026-11-21")
    check("withdrawn: kept as it was", data["offers"][TIERS]["status"], "partial")
    check(
        "changed: new spread",
        data["offers"][AJO]["contract"]["energy_price"]["f1"],
        0.06,
    )
    check("changed: first seen kept", data["offers"][AJO]["first_seen"], "2026-11-21")
    check("added: first seen", data["offers"][NEW]["first_seen"], "2026-11-22")
    check(
        "reported",
        sorted(out.split("\n")[:-1]),
        sorted(
            [
                f"added {NEW} FISSA SMART",
                f"changed {AJO} AJO LEGGERISSIMA ULTRA",
                f"withdrawn {TIERS} IREN SOTTOCASA LUCE NEW",
            ]
        ),
    )

    before = output.read_bytes()
    status, out, err = run(output, portale, "--today", "2026-11-22")
    check("same day again: status", status, 0)
    check("same day again: no change", out, "No change.\n")
    check("same day again: nothing on stderr", err, "")
    check("same day again: file untouched", output.read_bytes(), before)

    status, out, _ = run(output, portale, "--today", "2026-11-24")
    check("no file today: the latest of the days before", status, 0)
    check("no file today: no change", out, "No change.\n")
    check(
        "no file today: days tried, newest first",
        portale.asked[-3:],
        [u.file_url(date(2026, 11, day)) for day in (24, 23, 22)],
    )

    for item in data["offers"].values():
        if item["contract"] is not None:
            Contract.from_dict(item["contract"])
    print(f"PASS  every stored contract validates ({len(data['offers'])})")


def test_format(folder: Path) -> None:
    """One line per offer, so a daily commit touches only what changed."""
    output = folder / "offers.json"
    portale = Portale({date(2026, 11, 21): DAY_1})
    run(output, portale, "--today", "2026-11-21")
    text = output.read_text(encoding="utf-8")
    lines = text.split("\n")
    data = load(output)
    check(
        "lines: 6 of header, one per offer, 2 closing, final newline",
        len(lines),
        6 + len(data["offers"]) + 2 + 1,
    )
    codes = [line.split('"')[1] for line in lines if line.startswith('    "')]
    check("offers sorted by code", codes, sorted(data["offers"]))
    check("ends with a newline", text.endswith("}\n"), True)
    check("UTF-8, not escaped", "\\u" in text, False)


def test_refusals(folder: Path) -> None:
    """Nothing is written when the file is missing, broken or too short."""
    output = folder / "offers.json"
    run(output, Portale({date(2026, 11, 21): DAY_1}), "--today", "2026-11-21")
    before = output.read_bytes()

    status, out, err = run(output, Portale({}), "--today", "2026-11-23")
    check("no file in 4 days: status", status, 1)
    check("no file in 4 days: said", "Not updated" in err, True)
    check("no file in 4 days: untouched", output.read_bytes(), before)

    broken = Portale({date(2026, 11, 22): b"<html>Service unavailable</html"})
    status, out, err = run(output, broken, "--today", "2026-11-22")
    check("broken XML: status", status, 1)
    check("broken XML: said", "not valid XML" in err, True)
    check("broken XML: nothing on stdout", out, "")
    check("broken XML: untouched", output.read_bytes(), before)

    short = Portale({date(2026, 11, 22): TRUNCATED})
    status, _, err = run(output, short, "--today", "2026-11-22")
    check("half the offers gone: status", status, 1)
    check("half the offers gone: said", "2 of 18" in err, True)
    check("half the offers gone: untouched", output.read_bytes(), before)


def test_backfill(folder: Path) -> None:
    """Past days fill the archive; the newest content wins."""
    output = folder / "offers.json"
    portale = Portale(
        {
            date(2026, 11, 19): DAY_0,
            date(2026, 11, 21): DAY_1,
            date(2026, 11, 22): DAY_2,
        }
    )
    status, out, err = run(output, portale, "--backfill", "2026-11-20:2026-11-22")
    check("backfill: status", status, 0)
    check("backfill: missing days listed", "Missing: 2026-11-20" in err, True)
    data = load(output)
    check("backfill: updated to the last day", data["updated"], "2026-11-22")
    check("backfill: withdrawn", data["offers"][TIERS]["last_seen"], "2026-11-21")
    check(
        "backfill: changed", data["offers"][AJO]["contract"]["energy_price"]["f1"], 0.06
    )

    status, out, err = run(output, portale, "--backfill", "2026-11-19:2026-11-19")
    check("older day: status", status, 0)
    check("older day: gone offer reported", f"added {OLD} FISSA SMART" in out, True)
    data = load(output)
    check("older day: updated unchanged", data["updated"], "2026-11-22")
    tiers = data["offers"][TIERS]
    check("older day: first seen moves back", tiers["first_seen"], "2026-11-19")
    check("older day: last seen unchanged", tiers["last_seen"], "2026-11-21")
    check(
        "older day: newer content kept", tiers["contract"]["energy_price"]["f0"], 0.1793
    )
    old = data["offers"][OLD]
    check(
        "older day: gone offer archived",
        (old["first_seen"], old["last_seen"]),
        ("2026-11-19", "2026-11-19"),
    )
    check("older day: Ajo untouched", data["offers"][AJO]["first_seen"], "2026-11-21")

    status, _, _ = run(
        folder / "none.json", Portale({}), "--backfill", "2026-11-01:2026-11-02"
    )
    check("backfill with no file at all: status", status, 1)
    check(
        "backfill with no file at all: nothing written",
        (folder / "none.json").exists(),
        False,
    )


def main() -> int:
    """Run every test and report."""
    test_url()
    for test in (test_daily, test_format, test_refusals, test_backfill):
        with tempfile.TemporaryDirectory() as folder:
            try:
                test(Path(folder))
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
