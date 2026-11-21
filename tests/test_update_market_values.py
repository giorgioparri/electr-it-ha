# SPDX-License-Identifier: GPL-3.0-or-later
"""The updater of market_values.json. Run as a script:

    python3 tests/test_update_market_values.py

Nothing is downloaded and pypdf is not needed: the documents are built here,
with the wording and the layouts of Terna's and ARERA's real ones and the
2026 values that match the bills in test_bills.py.
"""

import io
import json
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import update_market_values as u

from engine.published import Published, PublishedValue

D = Decimal
_failures: list[str] = []


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


# --- Documents ----------------------------------------------------------------

# The order of the text as a PDF reader returns it: in order for some notices,
# with the date and the quarter after "è pari a" for others.
TIDE_IN_ORDER = """AVVISO AGLI UTENTI DEL DISPACCIAMENTO
Roma, 31 Dicembre 2025
Si comunica agli utenti del dispacciamento che il corrispettivo unitario di
dispacciamento relativo al  primo trimestre 202 6 di cui all'articolo 4 - 25.2
del Testo Integrato del Dispacciamento Elettrico (TIDE), è pari a:
1,0659000 centesimi di Euro/kWh
Di cui:
Corrispettivo uplift 0,2477000
"""
TIDE_SCRAMBLED = """AVVISO AGLI UTENTI DEL DISPACCIAMENTO
Si comunica agli utenti del dispacciamento che il corrispettivo unitario di dispacciamento
Di cui:
centesimi
di
Euro/kWh
relativo al di cui all'articolo 4 - 25.2 del Testo Integrato del
Dispacciamento Elettrico (TIDE) è pari a:
16 giugno 2026
terzo trimestre 2026
1,0501000
0,0761000
0,3326000
centesimi di Euro/kWh
"""
CAPACITY = """Corrispettivi unitari a copertura degli oneri netti di
approvvigionamento della capacità {q}° trimestre {year}
Terna rende noto il corrispettivo unitario per le ore diverse dalle
ore di picco del sistema elettrico per il {q}° trimestre {year}:
Corrispettivo unitario ore diverse dalle ore di picco del sistema elettrico → {value} €/MWh
• Importo di cui all'articolo 14.4 lettera a) → 1.219.462,55 €
"""

CELL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"


def workbook(sheets: dict[str, list[list[object]]]) -> bytes:
    """An .xlsx with one sheet per entry; text goes to the shared strings."""
    strings: list[str] = []

    def cell(value: object) -> str:
        if isinstance(value, str):
            strings.append(value)
            return f'<c t="s"><v>{len(strings) - 1}</v></c>'
        return f"<c><v>{value}</v></c>"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        listed, relations = [], []
        for n, (name, rows) in enumerate(sheets.items(), start=1):
            body = "".join(
                f"<row>{''.join(cell(value) for value in row)}</row>" for row in rows
            )
            archive.writestr(
                f"xl/worksheets/sheet{n}.xml",
                f'<worksheet xmlns="{CELL}"><sheetData>{body}</sheetData></worksheet>',
            )
            listed.append(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>')
            relations.append(
                f'<Relationship Id="rId{n}" Type="worksheet" '
                f'Target="worksheets/sheet{n}.xml"/>'
            )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{CELL}" xmlns:r="{REL}"><sheets>'
            f"{''.join(listed)}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE}">{"".join(relations)}</Relationships>',
        )
        items = "".join(f"<si><t>{text}</t></si>" for text in strings)
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{CELL}">{items}</sst>')
    return buffer.getvalue()


def month_sheet(
    title: str, cdispd: object, other: object | None = None
) -> list[list[object]]:
    """A sheet laid out as ARERA's: residents first, then the others."""
    header = ["dispacciamento", "sigma1", "sigma2", "sigma3"]
    return [
        [title],
        ["CLIENTI DOMESTICI CON FORNITURA NEL MERCATO LIBERO"],
        [" - Vendita di energia elettrica: Corrispettivo di dispacciamento (CDISPD)"],
        ["ABITAZIONI DI RESIDENZE ANAGRAFICHE"],
        header,
        ["Quota energia (euro/kWh)", cdispd, "- ", "- ", 0.0119],
        ["Quota fissa (euro/anno)", "- ", 23.04],
        ["ABITAZIONI DIVERSE DA RESIDENZE ANAGRAFICHE"],
        header,
        [
            "Quota energia (euro/kWh)",
            cdispd if other is None else other,
            "- ",
            "- ",
            0.0119,
        ],
    ]


ARERA_2026 = workbook(
    {
        "settembre 2026": month_sheet("Settembre 2026", 0.015068),
        "ottobre 2026": month_sheet("Ottobre 2026", 0.014642),
    }
)


def listing(*rows: tuple[str, str, str]) -> bytes:
    """An answer of Terna's list endpoint."""
    return json.dumps(
        {
            "Results": [
                {"Title": t, "PublicationDate": p, "Url": url} for t, p, url in rows
            ]
        }
    ).encode()


TIDE_LIST = listing(
    (
        "Avviso agli utenti del Dispacciamento - Pubblicazione Uplift mese Settembre 2026",
        "06/11/2026 00:00:00",
        "-/media/uplift-09.pdf",
    ),
    (
        "AVVISO AGLI UdD Pubblicazione 4Q2026",
        "17/09/2026 00:00:00",
        "-/media/tide-4q.pdf",
    ),
    (
        "AVVISO AGLI UdD Pubblicazione 3Q2026",
        "16/06/2026 00:00:00",
        "-/media/tide-3q.pdf",
    ),
)
CAPACITY_LIST = listing(
    (
        "Elenco delle ore in cui si è determinato il corrispettivo art 14.1 lettera c)",
        "16/09/2026 18:00:00",
        "-/media/hours.pdf",
    ),
    (
        "Corrispettivi unitari a copertura degli oneri netti di approvvigionamento della capacità 4° trimestre 2026 Terna",
        "16/09/2026 18:00:00",
        "-/media/cm-4q.pdf",
    ),
    (
        "Corrispettivi unitari a copertura degli oneri netti di  approvvigionamento della capacità 3° trimestre 2026",
        "16/06/2026 18:00:00",
        "-/media/cm-3q.pdf",
    ),
    (
        "Corrispettivi unitari a copertura degli oneri netti di approvvigionamento della capacità anno 2026",
        "15/12/2025 18:00:00",
        "-/media/cm-year.pdf",
    ),
)
DOCUMENTS: dict[str, bytes] = {
    u.terna_list_url(u.TIDE_CATEGORY, 2026): TIDE_LIST,
    u.terna_list_url(u.CAPACITY_CATEGORY, 2026): CAPACITY_LIST,
    f"{u.TERNA}/-/media/tide-3q.pdf": TIDE_SCRAMBLED.encode(),
    f"{u.TERNA}/-/media/tide-4q.pdf": TIDE_SCRAMBLED.replace("terzo", "quarto")
    .replace("1,0501000", "0,9888000")
    .encode(),
    f"{u.TERNA}/-/media/cm-3q.pdf": CAPACITY.format(
        q=3, year=2026, value="3,197"
    ).encode(),
    f"{u.TERNA}/-/media/cm-4q.pdf": CAPACITY.format(
        q=4, year=2026, value="3,423"
    ).encode(),
    u.ARERA_PAGE: b'<a href="/fileadmin/area_operatori/prezzi_e_tariffe/'
    b'Corrispettivi_libero_elettrico_domestico_2026.xlsx">qui</a>',
    u.ARERA_WORKBOOK.format(year=2026): ARERA_2026,
}


def site(documents: dict[str, bytes]) -> Callable[[str], bytes]:
    """A stand-in for the network: other lists are empty, the rest is missing."""

    def get(url: str) -> bytes:
        if url in documents:
            return documents[url]
        if url.startswith(u.TERNA_LIST):
            return b'{"Results": []}'
        raise u.UpdateError(f"404 {url}")

    return get


def as_text(data: bytes) -> str:
    """The documents above are text already: no PDF to read."""
    return data.decode()


# --- Cases --------------------------------------------------------------------


def test_numbers() -> None:
    """Italian numbers, and values written without trailing zeros."""
    check("thousands and decimals", u.italian_number("1.219.462,55"), D("1219462.55"))
    check("negative", u.italian_number("-0,0084000"), D("-0.0084000"))
    raises("not a number", u.UpdateError, u.italian_number, "n/d")
    check("cents with seven decimals", u.tidy(D("1.0501000") / 100), D("0.010501"))
    check("at least six decimals", str(u.tidy(D("0.01"))), "0.010000")


def test_tide() -> None:
    """The dispatching charge is the first number after 'è pari a', in cents."""
    check(
        "text in order, year split", u.tide_value(TIDE_IN_ORDER, 2026, 1), D("0.010659")
    )
    check(
        "date and quarter in between",
        u.tide_value(TIDE_SCRAMBLED, 2026, 3),
        D("0.010501"),
    )
    check(
        "it is read as six decimals",
        str(u.tide_value(TIDE_SCRAMBLED, 2026, 3)),
        "0.010501",
    )
    raises("another quarter", u.UpdateError, u.tide_value, TIDE_SCRAMBLED, 2026, 4)
    raises("another year", u.UpdateError, u.tide_value, TIDE_SCRAMBLED, 2027, 3)
    raises(
        "no unit",
        u.UpdateError,
        u.tide_value,
        TIDE_SCRAMBLED.replace("Euro/kWh", "Euro/MWh"),
        2026,
        3,
    )
    raises(
        "wording changed",
        u.UpdateError,
        u.tide_value,
        TIDE_SCRAMBLED.replace("è pari a", "vale"),
        2026,
        3,
    )


def test_capacity() -> None:
    """The capacity charge outside peak hours, from EUR/MWh."""
    text = CAPACITY.format(q=3, year=2026, value="3,197")
    check("third quarter", u.capacity_value(text, 2026, 3), D("0.003197"))
    raises("another quarter", u.UpdateError, u.capacity_value, text, 2026, 2)
    raises(
        "in another unit",
        u.UpdateError,
        u.capacity_value,
        text.replace("€/MWh", "€/kWh"),
        2026,
        3,
    )


def test_listing() -> None:
    """Notices are found by title; the last publication of a quarter counts."""
    notices = u.terna_notices(TIDE_LIST)
    check("three notices", len(notices), 3)
    check("absolute address", notices[1].url, f"{u.TERNA}/-/media/tide-4q.pdf")
    check("publication time", notices[1].published, datetime(2026, 9, 17))
    found = u.latest_by_quarter(notices, u._TIDE_TITLE)
    check("uplift notices skipped", sorted(found), [(2026, 3), (2026, 4)])
    corrections = u.terna_notices(
        listing(
            (
                "AVVISO AGLI UdD Pubblicazione 1Q2026_Aggiornamento 31 Dicembre 2025",
                "31/12/2025 00:00:00",
                "-/media/new.pdf",
            ),
            (
                "AVVISO AGLI UdD Pubblicazione 1Q2026",
                "23/12/2025 00:00:00",
                "-/media/old.pdf",
            ),
            (
                "AVVISO AGLI UdD Pubblicazione Corrispettivi Dispacciamento 1Q2025 - Provvisorio",
                "15/12/2024 00:00:00",
                "-/media/2025.pdf",
            ),
        )
    )
    latest = u.latest_by_quarter(corrections, u._TIDE_TITLE)
    check(
        "a correction replaces the notice",
        latest[(2026, 1)].url,
        f"{u.TERNA}/-/media/new.pdf",
    )
    check("a provisional notice is still read", (2025, 1) in latest, True)
    capacity = u.latest_by_quarter(u.terna_notices(CAPACITY_LIST), u._CAPACITY_TITLE)
    check(
        "yearly charges and hour lists skipped",
        sorted(capacity),
        [(2026, 3), (2026, 4)],
    )
    raises("not Terna's answer", u.UpdateError, u.terna_notices, b"<html>")
    check(
        "quarter bounds",
        u.quarter_bounds(2026, 4),
        (date(2026, 10, 1), date(2026, 12, 31)),
    )


def test_arera() -> None:
    """One CDispD per monthly sheet, the same for residents and the others."""
    months = u.arera_cdispd(ARERA_2026)
    check("two months", sorted(months), [(2026, 9), (2026, 10)])
    check("September", months[(2026, 9)][0], D("0.015068"))
    check("the sheet is named", months[(2026, 10)][1], "ottobre 2026")
    float_noise = workbook(
        {"giugno 2026": month_sheet("Giugno 2026", "0.019902000000000002")}
    )
    check(
        "binary noise cut to the millionth",
        u.arera_cdispd(float_noise)[(2026, 6)][0],
        D("0.019902"),
    )
    other_sheets = workbook(
        {"note": [["testo"]], "luglio 2026": month_sheet("Luglio 2026", 0.038464)}
    )
    check("sheets of no month skipped", list(u.arera_cdispd(other_sheets)), [(2026, 7)])
    disagree = workbook({"maggio 2026": month_sheet("Maggio 2026", 0.015531, 0.0155)})
    raises("residents and others disagree", u.UpdateError, u.arera_cdispd, disagree)
    no_cdispd = workbook({"maggio 2026": [["Quota energia (euro/kWh)", 0.01]]})
    raises("the CDispD is not mentioned", u.UpdateError, u.arera_cdispd, no_cdispd)
    raises("not a workbook", u.UpdateError, u.arera_cdispd, b"plain text")
    urls = u.arera_workbooks(DOCUMENTS[u.ARERA_PAGE], (2025, 2026))
    check(
        "linked workbook first, usual names after, no duplicates",
        urls,
        [u.ARERA_WORKBOOK.format(year=2026), u.ARERA_WORKBOOK.format(year=2025)],
    )


def test_collect() -> None:
    """Every source together, as the workflow runs it."""
    values = u.collect(site(DOCUMENTS), date(2026, 11, 21), as_text)
    by_name = {}
    for value in values:
        by_name.setdefault(value.name, []).append(value)
    check(
        "two quarters of dispatching",
        [v.eur_kwh for v in by_name["dispatching"]],
        [D("0.010501"), D("0.009888")],
    )
    check(
        "two quarters of capacity",
        [v.eur_kwh for v in by_name["capacity"]],
        [D("0.003197"), D("0.003423")],
    )
    check(
        "two months of CDispD",
        [v.eur_kwh for v in by_name["cdispd"]],
        [D("0.015068"), D("0.014642")],
    )
    check(
        "the source names the notice",
        "Pubblicazione 3Q2026" in by_name["dispatching"][0].source,
        True,
    )
    check(
        "the source names the sheet",
        "settembre 2026" in by_name["cdispd"][0].source,
        True,
    )
    check("they are sound", u.problems(values), [])
    no_arera = {k: v for k, v in DOCUMENTS.items() if "xlsx" not in k}
    raises(
        "no ARERA workbook at all",
        u.UpdateError,
        u.collect,
        site(no_arera),
        date(2026, 11, 21),
        as_text,
    )
    broken = {**DOCUMENTS, f"{u.TERNA}/-/media/cm-4q.pdf": b"changed layout"}
    raises(
        "a notice that cannot be read",
        u.UpdateError,
        u.collect,
        site(broken),
        date(2026, 11, 21),
        as_text,
    )


def value(name: str, start: date, end: date, eur_kwh: str) -> PublishedValue:
    """A value with a dummy source."""
    return PublishedValue(name, start, end, D(eur_kwh), "test")


Q3 = (date(2026, 7, 1), date(2026, 9, 30))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


def test_problems() -> None:
    """Implausible values and a CDispD below its own parts are caught."""
    sound = [
        value("dispatching", *Q3, "0.010501"),
        value("capacity", *Q3, "0.003197"),
        value("cdispd", *SEPTEMBER, "0.015068"),
    ]
    check("the September bill's values", u.problems(sound), [])
    peak = [
        *sound[:2],
        value("cdispd", date(2026, 7, 1), date(2026, 7, 31), "0.038464"),
    ]
    check("peak hours make July higher", u.problems(peak), [])
    low = [*sound[:2], value("cdispd", *SEPTEMBER, "0.0150")]
    check("a CDispD below its parts", len(u.problems(low)), 1)
    cents = [value("dispatching", *Q3, "1.0501")]
    check("cents read as euro", len(u.problems(cents)), 1)


def test_merge() -> None:
    """Fresh values replace old ones of the same period; the rest stays."""
    old = Published(
        date(2026, 10, 5),
        (
            value("cdispd", date(2026, 8, 1), date(2026, 8, 31), "0.018468"),
            value("cdispd", *SEPTEMBER, "0.015000"),
        ),
    )
    today = date(2026, 11, 21)
    same = u.merged(old, [value("cdispd", *SEPTEMBER, "0.015000")], today)
    check("nothing new: the very same content", same is old, True)
    new = u.merged(old, [value("cdispd", *SEPTEMBER, "0.015068")], today)
    check(
        "a revision replaces the value",
        new.for_month("cdispd", 2026, 9).eur_kwh,
        D("0.015068"),
    )
    check(
        "a value no longer listed stays",
        new.for_month("cdispd", 2026, 8) is not None,
        True,
    )
    check("the update date moves", new.updated, today)
    check(
        "the change is described",
        u.changes(old, new),
        ["changed cdispd 2026-09-01..2026-09-30: 0.015000 -> 0.015068"],
    )
    clash = [value("cdispd", date(2026, 9, 15), date(2026, 10, 14), "0.01")]
    raises("overlapping periods", u.UpdateError, u.merged, old, clash, today)


def test_overdue() -> None:
    """What should be out by a given day."""
    complete = Published(
        date(2026, 11, 21),
        (
            value("dispatching", date(2026, 10, 1), date(2026, 12, 31), "0.009888"),
            value("capacity", date(2026, 10, 1), date(2026, 12, 31), "0.003423"),
            value("cdispd", date(2026, 10, 1), date(2026, 10, 31), "0.014642"),
        ),
    )
    check("all there", u.overdue(complete, date(2026, 11, 21)), [])
    check(
        "last month's CDispD not due yet", u.overdue(complete, date(2026, 12, 10)), []
    )
    check(
        "and due later", u.overdue(complete, date(2026, 12, 21)), ["cdispd of 2026-11"]
    )
    check(
        "a new quarter, a few days of grace", u.overdue(complete, date(2027, 1, 2)), []
    )
    check(
        "then Terna is late",
        u.overdue(complete, date(2027, 1, 5)),
        ["dispatching of quarter 1 of 2027", "capacity of quarter 1 of 2027"],
    )


def test_main() -> None:
    """The file is written once, then left alone; problems write nothing."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "market_values.json"
        argv = ["--output", str(path), "--today", "2026-11-21"]
        check("first run", u.main(argv, site(DOCUMENTS), as_text), 0)
        written = path.read_text("utf-8")
        check("ends with a newline", written.endswith("}\n"), True)
        check(
            "it is a sound file",
            len(Published.from_dict(json.loads(written)).values),
            6,
        )
        check("second run", u.main(argv, site(DOCUMENTS), as_text), 0)
        check("unchanged", path.read_text("utf-8") == written, True)
        late = ["--output", str(path), "--today", "2027-01-10"]
        check("written but overdue", u.main(late, site(DOCUMENTS), as_text), 3)
        broken = {**DOCUMENTS, f"{u.TERNA}/-/media/tide-4q.pdf": b"changed layout"}
        check("unreadable notice", u.main(argv, site(broken), as_text), 1)
        check("the file was not touched", path.read_text("utf-8") == written, True)


def main() -> int:
    """Run every case and report."""
    for test in (
        test_numbers,
        test_tide,
        test_capacity,
        test_listing,
        test_arera,
        test_collect,
        test_problems,
        test_merge,
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
