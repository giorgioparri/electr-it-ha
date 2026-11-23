# SPDX-License-Identifier: GPL-3.0-or-later
"""The updater of the offers/ folder. Run as a script:

    python3 tests/test_update_offers.py

Nothing is downloaded: the daily files of the Portale are built here from the
real offers in tests/fixtures/portale_offerte_20261121.xml, and served by a
fake download keyed by URL.
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_offers as u

from engine.catalogue import INDEX_FILE, shard_key
from engine.contract import Contract

FIXTURE = Path(__file__).parent / "fixtures" / "portale_offerte_20261121.xml"
NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
SOURCE = "Portale Offerte (ARERA / Acquirente Unico), open data"
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


# --- The folder -------------------------------------------------------------------


def index(folder: Path) -> dict[str, Any]:
    """Read index.json."""
    return json.loads((folder / INDEX_FILE).read_text(encoding="utf-8"))


def offers(folder: Path) -> dict[str, Any]:
    """Every offer of the folder, by code, read file by file."""
    found: dict[str, Any] = {}
    for path in sorted(folder.glob("*.json")):
        if path.name != INDEX_FILE:
            found.update(json.loads(path.read_text(encoding="utf-8"))["offers"])
    return found


def shard(code: str) -> str:
    """The name of the file that holds `code`."""
    return f"{shard_key(code, 2)}.json"


def contents(folder: Path) -> dict[str, bytes]:
    """Every file of the folder, by name."""
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir())}


def age(folder: Path) -> None:
    """Set every file of the folder to the epoch, to tell later which are written."""
    for path in folder.iterdir():
        os.utime(path, ns=(0, 0))


def written(folder: Path) -> set[str]:
    """The files written since age()."""
    return {path.name for path in folder.iterdir() if path.stat().st_mtime_ns != 0}


# --- Tests ------------------------------------------------------------------------


def test_url() -> None:
    """The daily files live in one folder per month, its month not padded."""
    check(
        "URL of a daily file",
        u.file_url(date(2026, 11, 21)),
        "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/"
        "offerteML/2026_11/PO_Offerte_E_MLIBERO_20261121.xml",
    )
    # The folder of March is 2026_3, while the file name pads the month:
    # 2026_03 answers 404 (checked on the Portale on 22 November 2026).
    check(
        "URL of a daily file in a month before October",
        u.file_url(date(2026, 3, 5)),
        "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/"
        "offerteML/2026_3/PO_Offerte_E_MLIBERO_20260305.xml",
    )


def test_daily(root: Path) -> None:
    """Day after day: added, changed and withdrawn offers."""
    folder = root / "offers"
    portale = Portale({date(2026, 11, 21): DAY_1, date(2026, 11, 22): DAY_2})

    # A daily run never starts an archive on its own: with the index lost it
    # would keep the offers of one day only.
    status, out, err = run(folder, portale, "--today", "2026-11-21")
    check("no archive: status", status, 1)
    check("no archive: said", INDEX_FILE in err and "--init" in err, True)
    check("no archive: nothing created", folder.exists(), False)

    status, out, err = run(folder, portale, "--today", "2026-11-21", "--init")
    check("first day: status", status, 0)
    check(
        "first day: index",
        index(folder),
        {
            "schema_version": 2,
            "source": SOURCE,
            "updated": "2026-11-21",
            "file": "PO_Offerte_E_MLIBERO_20261121.xml",
            "shards": {"hash": "sha256", "digits": 2},
            "offers": 18,
            "on_sale": 18,
        },
    )
    data = offers(folder)
    check("first day: domestic offers", len(data), 18)
    check("first day: one line per offer", out.count("added "), 18)
    check(
        "first day: one file per hash, only where there are offers",
        set(contents(folder)) - {INDEX_FILE},
        {shard(code) for code in data},
    )
    entry = data[AJO]
    check("first day: first seen", entry["first_seen"], "2026-11-21")
    check("first day: on sale", entry["last_seen"], None)
    check("first day: converter", entry["converter"], u.CONVERTER)
    check("first day: status of the offer", entry["status"], "complete")
    check("first day: supplier", entry["info"]["supplier"], "ajoenergia.it")
    check("first day: power limit", entry["info"]["power_max_kw"], 6.0)
    check("first day: valid to", entry["info"]["valid_to"], "2026-11-22T23:59:59")
    check("first day: spread", entry["contract"]["energy_price"]["f1"], 0.05)

    age(folder)
    status, out, err = run(folder, portale, "--today", "2026-11-22")
    check("second day: status", status, 0)
    head = index(folder)
    check("second day: updated", head["updated"], "2026-11-22")
    check("second day: file", head["file"], "PO_Offerte_E_MLIBERO_20261122.xml")
    check("second day: offers counted", head["offers"], 19)
    check("second day: on sale counted", head["on_sale"], 18)
    data = offers(folder)
    check("second day: offers kept", len(data), 19)
    check("second day: other uses left out", BUSINESS in data, False)
    check(
        "second day: only the index and the files that changed are written",
        written(folder),
        {INDEX_FILE, shard(AJO), shard(TIERS), shard(NEW)},
    )
    check("withdrawn: last seen", data[TIERS]["last_seen"], "2026-11-21")
    check("withdrawn: kept as it was", data[TIERS]["status"], "partial")
    check("changed: new spread", data[AJO]["contract"]["energy_price"]["f1"], 0.06)
    check("changed: first seen kept", data[AJO]["first_seen"], "2026-11-21")
    check("added: first seen", data[NEW]["first_seen"], "2026-11-22")
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

    age(folder)
    status, out, err = run(folder, portale, "--today", "2026-11-22")
    check("same day again: status", status, 0)
    check("same day again: no change", out, "No change.\n")
    check("same day again: nothing on stderr", err, "")
    check("same day again: nothing written", written(folder), set())

    status, out, _ = run(folder, portale, "--today", "2026-11-24")
    check("no file today: the latest of the days before", status, 0)
    check("no file today: no change", out, "No change.\n")
    check(
        "no file today: days tried, newest first",
        portale.asked[-3:],
        [u.file_url(date(2026, 11, day)) for day in (24, 23, 22)],
    )

    for item in data.values():
        if item["contract"] is not None:
            Contract.from_dict(item["contract"])
    print(f"PASS  every stored contract validates ({len(data)})")


def test_format(root: Path) -> None:
    """One line per offer, so a daily commit touches only what changed."""
    folder = root / "offers"
    run(folder, Portale({date(2026, 11, 21): DAY_1}), "--today", "2026-11-21", "--init")
    text = (folder / shard(AJO)).read_text(encoding="utf-8")
    lines = text.split("\n")
    data = json.loads(text)
    check("shard: header", lines[:2], ["{", '  "schema_version": 2,'])
    check(
        "shard: 3 of header, one per offer, 2 closing, final newline",
        len(lines),
        3 + len(data["offers"]) + 2 + 1,
    )
    codes = [line.split('"')[1] for line in lines if line.startswith('    "')]
    check("shard: offers sorted by code", codes, sorted(data["offers"]))
    check("shard: ends with a newline", text.endswith("}\n"), True)
    for name, raw in contents(folder).items():
        check(f"{name}: UTF-8, not escaped", b"\\u" in raw, False)
    head = (folder / INDEX_FILE).read_text(encoding="utf-8")
    check("index: ends with a newline", head.endswith("}\n"), True)
    check("index: one key per line", head.count("\n") > 7, True)

    check(
        "the folder is what files() returns",
        {name: raw.decode() for name, raw in contents(folder).items()},
        u.files(u.merged(None, date(2026, 11, 21), u.read_day(DAY_1))),
    )


def test_refusals(root: Path) -> None:
    """Nothing is written when the file is missing, broken or too short."""
    folder = root / "offers"
    run(folder, Portale({date(2026, 11, 21): DAY_1}), "--today", "2026-11-21", "--init")
    before = contents(folder)

    def untouched(what: str) -> None:
        check(f"{what}: untouched", contents(folder), before)

    status, out, err = run(folder, Portale({}), "--today", "2026-11-23")
    check("no file in 4 days: status", status, 1)
    check("no file in 4 days: said", "Not updated" in err, True)
    untouched("no file in 4 days")

    broken = Portale({date(2026, 11, 22): b"<html>Service unavailable</html"})
    status, out, err = run(folder, broken, "--today", "2026-11-22")
    check("broken XML: status", status, 1)
    check("broken XML: said", "not valid XML" in err, True)
    check(
        "broken XML: the file named",
        "PO_Offerte_E_MLIBERO_20261122.xml" in err,
        True,
    )
    check("broken XML: nothing on stdout", out, "")
    untouched("broken XML")

    short = Portale({date(2026, 11, 22): TRUNCATED})
    status, _, err = run(folder, short, "--today", "2026-11-22")
    check("half the offers gone: status", status, 1)
    check("half the offers gone: said", "2 of 18" in err, True)
    untouched("half the offers gone")

    head = index(folder)
    head["schema_version"] = 1
    (folder / INDEX_FILE).write_text(json.dumps(head), encoding="utf-8")
    before = contents(folder)
    status, _, err = run(
        folder, Portale({date(2026, 11, 22): DAY_2}), "--today", "2026-11-22"
    )
    check("another schema: status", status, 1)
    check("another schema: said", "unsupported schema" in err, True)
    untouched("another schema")


def test_backfill(root: Path) -> None:
    """Past days fill the archive; the newest content wins."""
    folder = root / "offers"
    portale = Portale(
        {
            date(2026, 11, 19): DAY_0,
            date(2026, 11, 21): DAY_1,
            date(2026, 11, 22): DAY_2,
        }
    )
    status, out, err = run(
        folder, portale, "--backfill", "2026-11-20:2026-11-22", "--init"
    )
    check("backfill: status", status, 0)
    check("backfill: missing days listed", "Missing: 2026-11-20" in err, True)
    check("backfill: updated to the last day", index(folder)["updated"], "2026-11-22")
    data = offers(folder)
    check("backfill: withdrawn", data[TIERS]["last_seen"], "2026-11-21")
    check("backfill: changed", data[AJO]["contract"]["energy_price"]["f1"], 0.06)

    status, out, err = run(folder, portale, "--backfill", "2026-11-19:2026-11-19")
    check("older day: status", status, 0)
    check("older day: gone offer reported", f"added {OLD} FISSA SMART" in out, True)
    head = index(folder)
    check("older day: updated unchanged", head["updated"], "2026-11-22")
    check("older day: offers counted", head["offers"], 20)
    check("older day: on sale unchanged", head["on_sale"], 18)
    data = offers(folder)
    tiers = data[TIERS]
    check("older day: first seen moves back", tiers["first_seen"], "2026-11-19")
    check("older day: last seen unchanged", tiers["last_seen"], "2026-11-21")
    check(
        "older day: newer content kept", tiers["contract"]["energy_price"]["f0"], 0.1793
    )
    old = data[OLD]
    check(
        "older day: gone offer archived",
        (old["first_seen"], old["last_seen"]),
        ("2026-11-19", "2026-11-19"),
    )
    check("older day: Ajo untouched", data[AJO]["first_seen"], "2026-11-21")

    status, _, err = run(root / "fresh", portale, "--backfill", "2026-11-21:2026-11-22")
    check("backfill with no archive: status", status, 1)
    check("backfill with no archive: said", "--init" in err, True)
    check("backfill with no archive: nothing created", (root / "fresh").exists(), False)

    # The Portale serves some past days as an empty file: the day is
    # listed and skipped, the others are read.
    gaps = root / "gaps"
    portale = Portale(
        {
            date(2026, 11, 20): b"",
            date(2026, 11, 21): DAY_1,
            date(2026, 11, 22): DAY_2,
        }
    )
    status, out, err = run(
        gaps, portale, "--backfill", "2026-11-20:2026-11-22", "--init"
    )
    check("empty day: status", status, 0)
    check("empty day: listed", "Unreadable: 2026-11-20 (" in err, True)
    check("empty day: the other days read", index(gaps)["updated"], "2026-11-22")

    status, _, err = run(
        root / "empty",
        Portale({date(2026, 11, 20): b""}),
        "--backfill",
        "2026-11-20:2026-11-20",
        "--init",
    )
    check("only empty days: status", status, 1)
    check("only empty days: nothing written", (root / "empty").exists(), False)

    status, _, _ = run(
        root / "none", Portale({}), "--backfill", "2026-11-01:2026-11-02", "--init"
    )
    check("backfill with no file at all: status", status, 1)
    check(
        "backfill with no file at all: nothing written", (root / "none").exists(), False
    )


def test_convert(root: Path) -> None:
    """The offers.json of schema 1 becomes the folder a daily run would write."""
    model = u.merged(None, date(2026, 11, 21), u.read_day(DAY_1))
    model = u.merged(model, date(2026, 11, 22), u.read_day(DAY_2))
    old = root / "offers.json"
    old.write_text(
        json.dumps({**model, "schema_version": 1}, ensure_ascii=False),
        encoding="utf-8",
    )

    folder = root / "offers"
    status, out, err = run(folder, Portale({}), "--convert", str(old))
    check("convert: status", status, 0)
    check("convert: said", "19 offers" in out, True)
    check(
        "convert: same files as the daily runs",
        {name: raw.decode() for name, raw in contents(folder).items()},
        u.files(model),
    )

    before = contents(folder)
    status, _, err = run(folder, Portale({}), "--convert", str(old))
    check("convert over an archive: status", status, 1)
    check("convert over an archive: said", INDEX_FILE in err, True)
    check("convert over an archive: untouched", contents(folder), before)

    newer = root / "newer.json"
    newer.write_text(json.dumps({**model, "schema_version": 2}), encoding="utf-8")
    status, _, err = run(root / "other", Portale({}), "--convert", str(newer))
    check("convert another schema: status", status, 1)
    check("convert another schema: said", "unsupported schema" in err, True)
    check("convert another schema: nothing created", (root / "other").exists(), False)


def main() -> int:
    """Run every test and report."""
    test_url()
    for test in (test_daily, test_format, test_refusals, test_backfill, test_convert):
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
