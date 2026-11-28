# SPDX-License-Identifier: GPL-3.0-or-later
"""The updater of regulated_values.json. Run as a script:

    python3 tests/test_update_portale_parameters.py

Nothing is downloaded: the Portale is replaced by a dictionary of files,
built from its real parameters file of 27 November 2026 in tests/fixtures.
"""

import json
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_portale_parameters as p

from engine.published_rates import PublishedRates
from engine.regulated import RATES, Component, rate

D = Decimal
C = Component
_failures: list[str] = []

FIXTURE = Path(__file__).parent / "fixtures" / "portale_parametri_20261127.csv"
REAL = FIXTURE.read_bytes()
DAY = date(2026, 11, 27)


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
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


def edited(name: str, value: str) -> bytes:
    """The real file with the value of one parameter changed."""
    lines = REAL.decode("utf-8").splitlines(keepends=True)
    for position, line in enumerate(lines):
        if line.startswith(f"{name},"):
            rest = line.split(",", 2)[2]
            lines[position] = f"{name},{value},{rest}"
            return "".join(lines).encode("utf-8")
    raise AssertionError(f"{name} is not in the fixture")


def without(name: str) -> bytes:
    """The real file without one parameter."""
    lines = REAL.decode("utf-8").splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith(f"{name},")).encode()


def portale(files: dict[date, bytes]) -> Callable[[str], bytes]:
    """A fake Portale serving one file per day; 404 for the other days."""
    by_url = {p.file_url(day): data for day, data in files.items()}

    def get(url: str) -> bytes:
        if url not in by_url:
            raise p.NotFoundError(f"{url}: not published")
        return by_url[url]

    return get


def test_address() -> None:
    """The file of a day sits in the folder of its month."""
    check("name", p.file_name(DAY), "PO_Parametri_Mercato_Libero_E_20261127.csv")
    check(
        "address",
        p.file_url(DAY),
        "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/"
        "parametriML/2026_11/PO_Parametri_Mercato_Libero_E_20261127.csv",
    )


def test_parse() -> None:
    """The CSV is read as parameter -> number."""
    params = p.parse(REAL)
    check("sigma1", params["sigma1"], D("23.04"))
    check("iva_c", params["iva_c"], D("0.1"))
    check("a quoted description with commas", params["terna"], D("0.000717"))
    check("uc6s_d", params["uc6s_d"], D("0.1988"))
    check("a BOM is accepted", p.parse(b"\xef\xbb\xbf" + REAL), params)
    raises("not UTF-8", p.UpdateError, p.parse, b"\xff\xfe")
    raises("empty", p.UpdateError, p.parse, b"")
    raises("other header", p.UpdateError, p.parse, b"name,value\nsigma1,1\n")
    raises(
        "no rows",
        p.UpdateError,
        p.parse,
        b"nome_parametro,valore,descrizione\n",
    )
    raises("not a number", p.UpdateError, p.parse, edited("sigma1", "23,04"))
    raises(
        "twice",
        p.UpdateError,
        p.parse,
        REAL + b"sigma1,23.040000,Distribuzione (Sigma1)\n",
    )


def test_regulated_values() -> None:
    """Thirteen values, the same as the built-in table for the fourth quarter."""
    values = p.regulated_values(p.parse(REAL))
    check("thirteen values", len(values), 13)
    for component, value in values.items():
        check(f"{component}", value, rate(component, DAY, RATES))
    check("written without trailing zeros", str(values[C.VAT_RATE]), "0.1")
    raises("missing", p.UpdateError, p.regulated_values, p.parse(without("asos_dr")))
    for name, value in (
        ("acc_c_r_l", "0.000000"),
        ("acc_c_r_h", "0.025000"),
        ("asos_dnr_v", "0.030000"),
        ("arim_dnr_v", "0.002000"),
        ("arim_dnr_f", "1.000000"),
        ("lambda", "0.102000"),
        ("sigma3", "-0.011900"),
    ):
        raises(
            f"{name} = {value}",
            p.UpdateError,
            p.regulated_values,
            p.parse(edited(name, value)),
        )


def test_archive() -> None:
    """The raw file is kept only when its content changes."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "portale-parameters"
        first = p.archive(root, DAY, REAL)
        check(
            "first file kept", None if first is None else first.name, p.file_name(DAY)
        )
        check("same content", p.archive(root, date(2026, 11, 28), REAL), None)
        other = edited("asos_dr", "0.032000")
        later = p.archive(root, date(2026, 12, 1), other)
        check(
            "changed content",
            None if later is None else later.name,
            p.file_name(date(2026, 12, 1)),
        )
        check(
            "back to the old content is a change",
            p.archive(root, date(2026, 12, 2), REAL) is not None,
            True,
        )
        check("files", len(list(root.iterdir())), 3)


def test_overdue() -> None:
    """From day 5 to day 20 of a quarter its values must be there and updated."""
    q4 = p.regulated_values(p.parse(REAL))
    read = PublishedRates.empty().observe(DAY, q4, "27 Nov")
    check("mid-quarter", p.overdue(read, date(2026, 12, 15)), [])
    check("within the grace days", p.overdue(read, date(2027, 1, 4)), [])
    check("missing quarter", len(p.overdue(read, date(2027, 1, 6))), 1)
    check("after the watch", p.overdue(read, date(2027, 1, 22)), [])
    late = read.observe(date(2027, 1, 2), q4, "2 Jan")
    check("unchanged quarter", len(p.overdue(late, date(2027, 1, 6))), 1)
    updated = late.observe(
        date(2027, 1, 7), {**q4, C.ASOS_EUR_KWH: D("0.0325")}, "7 Jan"
    )
    check("updated quarter", p.overdue(updated, date(2027, 1, 7)), [])
    check("unchanged but too late to say", p.overdue(late, date(2027, 1, 25)), [])


def test_main() -> None:
    """The archive is written once, then left alone; problems write nothing."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        output = root / "regulated_values.json"
        raw = root / "portale-parameters"
        argv = ["--output", str(output), "--archive", str(raw)]
        site = portale({DAY: REAL})

        check("first run", p.main([*argv, "--today", "2026-11-27"], site), 0)
        written = output.read_text("utf-8")
        check("ends with a newline", written.endswith("}\n"), True)
        archive = PublishedRates.from_dict(json.loads(written))
        check("thirteen entries", len(archive.entries), 13)
        check("raw file kept", [f.name for f in raw.iterdir()], [p.file_name(DAY)])

        check(
            "next day, file not out yet",
            p.main([*argv, "--today", "2026-11-28"], site),
            0,
        )
        check("unchanged", output.read_text("utf-8") == written, True)
        check("nothing more kept", len(list(raw.iterdir())), 1)

        check("nothing for days", p.main([*argv, "--today", "2026-12-05"], site), 1)
        bad = portale({DAY: edited("acc_c_r_l", "0.000000")})
        check(
            "a file that does not fit", p.main([*argv, "--today", "2026-11-27"], bad), 1
        )
        check("the archive was not touched", output.read_text("utf-8") == written, True)
        check("the bad file was not kept", len(list(raw.iterdir())), 1)

        january = portale({date(2027, 1, 6): REAL})
        check("written but stale", p.main([*argv, "--today", "2027-01-06"], january), 3)
        archive = PublishedRates.from_dict(json.loads(output.read_text("utf-8")))
        check("a second quarter", len(archive.entries), 26)


def main() -> int:
    """Run every case and report."""
    for test in (
        test_address,
        test_parse,
        test_regulated_values,
        test_archive,
        test_overdue,
        test_main,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
