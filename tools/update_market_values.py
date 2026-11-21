#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Update market_values.json from the publications of Terna and ARERA.

Run by a scheduled workflow, not by Home Assistant. It reads:

- from Terna (dati.terna.it), the notices of the dispatching charge under the
  TIDE and of the capacity market charge, one PDF per quarter. Terna lists
  them through the JSON endpoint behind its "Corrispettivi" page; a notice
  can be published again with corrections, and the latest one counts;
- from ARERA, the workbook of the charges for household customers in the
  free market, one sheet per month, whose first column is the CDispD.

Every value is checked before anything is written. Exit status:

- 0: the file is up to date (written or unchanged);
- 1: something could not be read or a value looks wrong: nothing is written;
- 3: the file was written, but a value that should be out by now is missing.

Usage: python3 tools/update_market_values.py [--output PATH] [--today YYYY-MM-DD]

Needs pypdf to read the PDFs (pip install pypdf); the rest is the standard
library, so the parsing functions can be tested without it.
"""

import argparse
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "custom_components" / "electr_it_ha"))

from engine.market import CAPACITY, CDISPD, DISPATCHING  # noqa: E402
from engine.published import (  # noqa: E402
    Published,
    PublishedDataError,
    PublishedValue,
    month_bounds,
)

OUTPUT = REPOSITORY / "custom_components" / "electr_it_ha" / "market_values.json"
USER_AGENT = "electrITHA market values (+https://github.com/giorgioparri/electr-it-ha)"
TIMEOUT_SECONDS = 60

TERNA = "https://dati.terna.it"
TERNA_LIST = f"{TERNA}/api/sitecore/dati/corrispettivi/get"
TIDE_CATEGORY = "{E09352AE-CE30-4190-8956-F534D3EC4370}"
CAPACITY_CATEGORY = "{EF47860A-9A26-4C5E-9FB9-408EFA3E970E}"
_TERNA_AREA = "{8E598470-143A-4E83-B7C4-90AC78CAF309}"
_TERNA_UP_TYPE = "{E42B82CA-A1C3-4A88-8A49-DF8819924988}"

ARERA = "https://www.arera.it"
ARERA_PAGE = f"{ARERA}/consumatori/valori-rete-oneri-domestici-ee"
ARERA_WORKBOOK = (
    f"{ARERA}/fileadmin/area_operatori/prezzi_e_tariffe/"
    "Corrispettivi_libero_elettrico_domestico_{year}.xlsx"
)

# Values outside these bounds, in EUR/kWh, are reading errors, not prices.
BOUNDS = {
    DISPATCHING: (Decimal("-0.01"), Decimal("0.05")),
    CAPACITY: (Decimal(0), Decimal("0.05")),
    CDISPD: (Decimal(0), Decimal("0.2")),
}
LOSS_FACTOR = Decimal("1.10")
_MICRO = Decimal("0.000001")

# A value of the running quarter or of last month is overdue after these days.
QUARTER_GRACE_DAYS = 3
MONTH_GRACE_DAYS = 20

_ORDINALS = {1: "primo", 2: "secondo", 3: "terzo", 4: "quarto"}
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "gennaio",
            "febbraio",
            "marzo",
            "aprile",
            "maggio",
            "giugno",
            "luglio",
            "agosto",
            "settembre",
            "ottobre",
            "novembre",
            "dicembre",
        ),
        start=1,
    )
}

type Fetch = Callable[[str], bytes]


class UpdateError(Exception):
    """A publication cannot be read, or what it says cannot be trusted."""


def fetch(url: str) -> bytes:
    """Download `url`; UpdateError when the server does not answer with it."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as err:
        raise UpdateError(f"cannot download {url}: {err}") from err


# --- Text helpers -----------------------------------------------------------


def flat(text: str) -> str:
    """Collapse every run of whitespace into one space."""
    return re.sub(r"\s+", " ", text)


def _year(year: int) -> str:
    """A pattern for `year` that tolerates a PDF splitting it ("202 6")."""
    return r" ?".join(str(year))


def italian_number(text: str) -> Decimal:
    """Read a number written the Italian way: "1.219,55" or "-0,0084000"."""
    cleaned = text.strip().replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise UpdateError(f"'{text}' is not a number") from None


def tidy(value: Decimal) -> Decimal:
    """Drop trailing zeros, keeping at least six decimals and no exponent."""
    exponent = min(value.normalize().as_tuple().exponent, -6)
    return value.quantize(Decimal(1).scaleb(exponent))


def pdf_text(data: bytes) -> str:
    """Return the text of every page of a PDF."""
    from pypdf import PdfReader  # only the workflow needs it, not the tests

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        raise UpdateError(f"unreadable PDF: {err}") from err


# --- Terna --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """One document listed by Terna."""

    title: str
    published: datetime
    url: str


def terna_list_url(category: str, year: int) -> str:
    """Return the address of the list of a category's documents for a year."""
    query = urllib.parse.urlencode(
        {
            "datasource": category,
            "year": year,
            "month": 0,
            "day": "",
            "datePublishing": "",
            "type": "",
            "area": _TERNA_AREA,
            "pageNumber": 0,
            "pageSize": 100,
            "orderBy": 0,
            "orderDirection": "desc",
            "language": "it-IT",
            "searchTerm": "",
            "upType": _TERNA_UP_TYPE,
            "quarter": "",
        }
    )
    return f"{TERNA_LIST}?{query}"


def terna_notices(answer: bytes) -> list[Notice]:
    """Read the documents in an answer of Terna's list endpoint."""
    try:
        results = json.loads(answer)["Results"]
        return [
            Notice(
                " ".join(str(item["Title"]).split()),
                datetime.strptime(item["PublicationDate"], "%d/%m/%Y %H:%M:%S"),
                urllib.parse.urljoin(f"{TERNA}/", str(item["Url"])),
            )
            for item in results
        ]
    except (ValueError, KeyError, TypeError) as err:
        raise UpdateError(f"unexpected answer from Terna's list: {err}") from err


# "1Q2026_Aggiornamento ...": an underscore may follow, so no \b at the end.
_TIDE_TITLE = re.compile(
    r"\bPubblicazione\b.*?(?<![0-9A-Za-z])([1-4])Q(\d{4})(?!\d)", re.IGNORECASE
)
_CAPACITY_TITLE = re.compile(
    r"^Corrispettivi unitari\b.*?\b([1-4])°\s*trimestre\s+(\d{4})\b", re.IGNORECASE
)


def latest_by_quarter(
    notices: Iterable[Notice], title: re.Pattern[str]
) -> dict[tuple[int, int], Notice]:
    """Keep, for each (year, quarter), the notice published last.

    Terna publishes a notice again when it corrects it ("Aggiornamento", or a
    provisional one replaced by the final one): the latest is in force.
    """
    latest: dict[tuple[int, int], Notice] = {}
    for notice in notices:
        if "uplift" in notice.title.lower():
            continue  # the monthly uplift after the fact; bills use the quarter
        match = title.search(notice.title)
        if match is None:
            continue
        key = (int(match.group(2)), int(match.group(1)))
        if key not in latest or notice.published > latest[key].published:
            latest[key] = notice
    return latest


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    """Return the first and the last day of a quarter."""
    first_month = 3 * (quarter - 1) + 1
    return month_bounds(year, first_month)[0], month_bounds(year, first_month + 2)[1]


def tide_value(text: str, year: int, quarter: int) -> Decimal:
    """Read the dispatching charge of a quarter, in EUR/kWh, from its notice.

    The notice gives it in cents of EUR/kWh, as the first number after
    "è pari a". Depending on how the PDF was made, the text of the date and
    of the quarter may come between the two.
    """
    text = flat(text)
    if not re.search(rf"\b{_ORDINALS[quarter]} trimestre {_year(year)}\b", text, re.I):
        raise UpdateError(f"the notice is not about quarter {quarter} of {year}")
    if "centesimi di Euro/kWh" not in text:
        raise UpdateError("the notice does not give cents of EUR/kWh")
    _, separator, tail = text.partition("è pari a")
    if not separator:
        raise UpdateError("the notice does not say 'è pari a'")
    match = re.search(r"-?\d+,\d+", tail)
    if match is None:
        raise UpdateError("no value after 'è pari a'")
    return tidy(italian_number(match.group()) / 100)


def capacity_value(text: str, year: int, quarter: int) -> Decimal:
    """Read the capacity market charge outside peak hours, in EUR/kWh."""
    text = flat(text)
    if not re.search(rf"\b{quarter}° trimestre {_year(year)}\b", text):
        raise UpdateError(f"the notice is not about quarter {quarter} of {year}")
    match = re.search(
        r"ore diverse dalle ore di picco del sistema elettrico\W*?"
        r"(-?\d[\d.]*,\d+) ?€ ?/ ?MWh",
        text,
    )
    if match is None:
        raise UpdateError("no charge outside peak hours in EUR/MWh")
    return tidy(italian_number(match.group(1)) / 1000)


def terna_values(
    get: Fetch, years: Iterable[int], read_pdf: Callable[[bytes], str] = pdf_text
) -> list[PublishedValue]:
    """Read the dispatching and capacity charges of every quarter listed."""
    values: list[PublishedValue] = []
    for name, category, title, read in (
        (DISPATCHING, TIDE_CATEGORY, _TIDE_TITLE, tide_value),
        (CAPACITY, CAPACITY_CATEGORY, _CAPACITY_TITLE, capacity_value),
    ):
        notices = [
            notice
            for year in years
            for notice in terna_notices(get(terna_list_url(category, year)))
        ]
        for (year, quarter), notice in sorted(
            latest_by_quarter(notices, title).items()
        ):
            try:
                value = read(read_pdf(get(notice.url)), year, quarter)
            except UpdateError as err:
                raise UpdateError(f"{notice.title} ({notice.url}): {err}") from err
            start, end = quarter_bounds(year, quarter)
            source = (
                f"Terna, '{notice.title}', published "
                f"{notice.published:%Y-%m-%d}, {notice.url}"
            )
            values.append(PublishedValue(name, start, end, value, source))
    return values


# --- ARERA --------------------------------------------------------------------

_SPREADSHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATIONSHIP = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _sheets(archive: zipfile.ZipFile) -> list[tuple[str, list[list[str]]]]:
    """Return each sheet's name and its rows, as the text of their cells."""
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(node.text or "" for node in item.iter(f"{_SPREADSHEET}t"))
            for item in root.iter(f"{_SPREADSHEET}si")
        ]
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.get("Id"): item.get("Target")
        for item in relations.iter(f"{_PACKAGE}Relationship")
    }
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets: list[tuple[str, list[list[str]]]] = []
    for sheet in workbook.iter(f"{_SPREADSHEET}sheet"):
        target = str(targets[sheet.get(f"{_RELATIONSHIP}id")]).lstrip("/")
        path = target if target.startswith("xl/") else f"xl/{target}"
        rows: list[list[str]] = []
        for row in ET.fromstring(archive.read(path)).iter(f"{_SPREADSHEET}row"):
            cells: list[str] = []
            for cell in row.iter(f"{_SPREADSHEET}c"):
                value = cell.find(f"{_SPREADSHEET}v")
                if value is None or value.text is None:
                    inline = cell.find(f"{_SPREADSHEET}is")
                    if inline is not None:
                        cells.append(
                            "".join(
                                t.text or "" for t in inline.iter(f"{_SPREADSHEET}t")
                            )
                        )
                    continue
                cells.append(
                    shared[int(value.text)] if cell.get("t") == "s" else value.text
                )
            rows.append(cells)
        sheets.append((str(sheet.get("name")), rows))
    return sheets


def arera_cdispd(workbook: bytes) -> dict[tuple[int, int], tuple[Decimal, str]]:
    """Read the CDispD of each month in ARERA's workbook, in EUR/kWh.

    One sheet per month, named after it ("ottobre 2026"). The CDispD is the
    first number of the "Quota energia" rows, once for residents and once
    for the others: the two must agree.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(workbook))
        sheets = _sheets(archive)
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError) as err:
        raise UpdateError(f"unreadable workbook: {err}") from err
    months: dict[tuple[int, int], tuple[Decimal, str]] = {}
    for name, rows in sheets:
        match = re.fullmatch(r"\s*([a-zà]+)\s+(\d{4})\s*", name.lower())
        if match is None or match.group(1) not in _MONTHS:
            continue
        if not any("CDISPD" in cell.upper() for row in rows for cell in row):
            raise UpdateError(f"sheet '{name}' does not mention the CDispD")
        found: set[Decimal] = set()
        for row in rows:
            if row and row[0].strip().lower().startswith("quota energia"):
                for cell in row[1:]:
                    try:
                        found.add(Decimal(cell.strip()).quantize(_MICRO))
                        break
                    except InvalidOperation:
                        continue
        if len(found) != 1:
            raise UpdateError(
                f"sheet '{name}': expected one CDispD, found {sorted(found)}"
            )
        key = (int(match.group(2)), _MONTHS[match.group(1)])
        months[key] = (found.pop(), name.strip())
    return months


def arera_workbooks(page: bytes, years: Iterable[int]) -> list[str]:
    """Return the workbooks linked from ARERA's page, plus the usual names."""
    linked = re.findall(
        rb"""href=["']([^"']*Corrispettivi_libero_elettrico_domestico_\d{4}\.xlsx)["']""",
        page,
    )
    urls = [urllib.parse.urljoin(ARERA_PAGE, link.decode()) for link in linked]
    urls += [ARERA_WORKBOOK.format(year=year) for year in years]
    return list(dict.fromkeys(urls))


def arera_values(get: Fetch, years: Iterable[int]) -> list[PublishedValue]:
    """Read the CDispD of every month published in the workbooks found."""
    years = list(years)
    try:
        page = get(ARERA_PAGE)
    except UpdateError:
        page = b""  # the usual file names may still work
    months: dict[tuple[int, int], tuple[Decimal, str, str]] = {}
    read_any = False
    for url in arera_workbooks(page, years):
        try:
            data = get(url)
        except UpdateError:
            continue  # a year whose workbook does not exist (yet)
        read_any = True
        for key, (value, sheet) in arera_cdispd(data).items():
            months[key] = (value, sheet, url)
    if not read_any:
        raise UpdateError("no ARERA workbook could be downloaded")
    values = []
    for (year, month), (value, sheet, url) in sorted(months.items()):
        start, end = month_bounds(year, month)
        source = f"ARERA, sheet '{sheet}' of {url}"
        values.append(PublishedValue(CDISPD, start, end, value, source))
    return values


# --- Checks and merge ---------------------------------------------------------


def problems(values: Iterable[PublishedValue]) -> list[str]:
    """Return what makes the values untrustworthy (empty when they are sound).

    Besides plausible bounds, ARERA's CDispD of a month is Terna's
    dispatching plus capacity charge times 1.10, plus a share of the
    capacity charge for peak hours in months that have them: never less.
    """
    values = list(values)
    found: list[str] = []
    for value in values:
        low, high = BOUNDS[value.name]
        if not low <= value.eur_kwh <= high:
            found.append(
                f"{value.name} from {value.valid_from}: {value.eur_kwh} EUR/kWh "
                f"is outside {low}..{high}"
            )

    def covering(name: str, day: date) -> PublishedValue | None:
        return next(
            (v for v in values if v.name == name and v.valid_from <= day <= v.valid_to),
            None,
        )

    for value in values:
        if value.name != CDISPD:
            continue
        dispatching = covering(DISPATCHING, value.valid_from)
        capacity = covering(CAPACITY, value.valid_from)
        if dispatching is None or capacity is None:
            continue
        base = ((dispatching.eur_kwh + capacity.eur_kwh) * LOSS_FACTOR).quantize(_MICRO)
        if value.eur_kwh < base - _MICRO:
            found.append(
                f"cdispd of {value.valid_from:%Y-%m} is {value.eur_kwh}, less than "
                f"(dispatching + capacity) x 1.10 = {base}"
            )
    return found


def merged(
    old: Published | None, fresh: Iterable[PublishedValue], today: date
) -> Published:
    """Add the fresh values to the old ones; a fresh value replaces an old one.

    Values no longer listed by the sources stay. When nothing changes, the
    old content is returned as it is, update date included.
    """
    by_key = {(v.name, v.valid_from): v for v in (old.values if old else ())}
    for value in fresh:
        by_key[(value.name, value.valid_from)] = value
    values = tuple(sorted(by_key.values(), key=lambda v: (v.name, v.valid_from)))
    if old is not None:
        current = tuple(sorted(old.values, key=lambda v: (v.name, v.valid_from)))
        if current == values:
            return old
    try:
        return Published(today, values)
    except PublishedDataError as err:
        raise UpdateError(f"the values would contradict each other: {err}") from err


def overdue(published: Published, today: date) -> list[str]:
    """Return the values that should be out by `today` but are missing."""
    missing: list[str] = []
    quarter = (today.month - 1) // 3 + 1
    start, _ = quarter_bounds(today.year, quarter)
    if today >= start + timedelta(days=QUARTER_GRACE_DAYS):
        for name in (DISPATCHING, CAPACITY):
            if published.for_month(name, today.year, start.month) is None:
                missing.append(f"{name} of quarter {quarter} of {today.year}")
    if today.day > MONTH_GRACE_DAYS:
        last = date(today.year, today.month, 1) - timedelta(days=1)
        if published.for_month(CDISPD, last.year, last.month) is None:
            missing.append(f"cdispd of {last:%Y-%m}")
    return missing


def changes(old: Published | None, new: Published) -> list[str]:
    """Describe the difference between two contents, one line per value."""
    before = {(v.name, v.valid_from): v for v in (old.values if old else ())}
    lines = []
    for value in sorted(new.values, key=lambda v: (v.name, v.valid_from)):
        previous = before.get((value.name, value.valid_from))
        if previous == value:
            continue
        period = f"{value.valid_from}..{value.valid_to}"
        if previous is None:
            lines.append(f"added {value.name} {period}: {value.eur_kwh}")
        else:
            lines.append(
                f"changed {value.name} {period}: {previous.eur_kwh} -> {value.eur_kwh}"
            )
    return lines


# --- Command line -------------------------------------------------------------


def collect(
    get: Fetch, today: date, read_pdf: Callable[[bytes], str] = pdf_text
) -> list[PublishedValue]:
    """Read every value the sources publish around `today`."""
    terna_years = (today.year - 1, today.year, today.year + 1)
    arera_years = (today.year - 1, today.year)
    return [
        *terna_values(get, terna_years, read_pdf),
        *arera_values(get, arera_years),
    ]


def write(path: Path, published: Published) -> None:
    """Write the file the way it is kept in the repository."""
    text = json.dumps(published.to_dict(), indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def main(
    argv: list[str] | None = None,
    get: Fetch = fetch,
    read_pdf: Callable[[bytes], str] = pdf_text,
) -> int:
    """Update the file; see the module docstring for the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    try:
        old = None
        if args.output.exists():
            old = Published.from_dict(json.loads(args.output.read_text("utf-8")))
        fresh = collect(get, args.today, read_pdf)
        found = problems(fresh)
        if found:
            raise UpdateError("; ".join(found))
        new = merged(old, fresh, args.today)
    except (UpdateError, PublishedDataError, ValueError) as err:
        print(f"Not updated: {err}", file=sys.stderr)
        return 1

    if new is old:
        print("No change.")
    else:
        write(args.output, new)
        print("\n".join(changes(old, new)))
    missing = overdue(new, args.today)
    if missing:
        print(f"Overdue: {', '.join(missing)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
