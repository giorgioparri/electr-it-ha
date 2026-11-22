#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Update offers.json from the open data of the Portale Offerte.

Run by a scheduled workflow, not by Home Assistant. Every day the Portale
Offerte (ARERA, Acquirente Unico) publishes the electricity offers of the free
market as one XML file, kept in a folder per month. This reads the file of the
day, converts every offer for households with engine/offers.py, and merges
the result into offers.json:

- an offer keeps the date it was first seen; while it is on sale its
  "last_seen" is null, and once it leaves the Portale it records the last day
  it was there. Withdrawn offers stay, for the bills that still carry their
  code;
- the content of an offer is the one of the most recent day it was seen.

With --backfill, the daily files of a range of days are read in order, to
fill the archive once; days with no file, or with a file that cannot be read
(the Portale serves some past days as an empty file), are listed and skipped.

Exit status: 0 when the file is up to date (written or unchanged); 1 when
nothing could be read, or what was read cannot be trusted: nothing is written.

Usage:
    python3 tools/update_offers.py [--output PATH] [--today YYYY-MM-DD]
    python3 tools/update_offers.py [--output PATH] --backfill FROM:TO

Standard library only.
"""

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "custom_components" / "electr_it_ha"))

from engine.offers import Conversion, OfferInfo, convert, parse_offers  # noqa: E402

OUTPUT = Path("offers.json")
SCHEMA_VERSION = 1
# Bump when engine/offers.py converts differently: the archive records, for
# each offer, the converter that produced it.
CONVERTER = 1
SOURCE = "Portale Offerte (ARERA / Acquirente Unico), open data"
PORTALE = (
    "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/offerteML"
)
USER_AGENT = "electrITHA offers (+https://github.com/giorgioparri/electr-it-ha)"
TIMEOUT_SECONDS = 120

# Days before today to look back to when today's file is not out yet.
LOOK_BACK_DAYS = 3
# A file holding less than this share of the offers on sale is not trusted.
MIN_SHARE = Decimal("0.5")

type Fetch = Callable[[str], bytes]
type Entry = dict[str, Any]


class UpdateError(Exception):
    """A file cannot be read, or what it says cannot be trusted."""


class NotFoundError(UpdateError):
    """The Portale has no file at that address."""


def fetch(url: str) -> bytes:
    """Download `url`; NotFoundError on 404, UpdateError on any other failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise NotFoundError(f"no file at {url}") from err
        raise UpdateError(f"cannot download {url}: {err}") from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise UpdateError(f"cannot download {url}: {err}") from err


def file_name(day: date) -> str:
    """The name of the daily file of `day`."""
    return f"PO_Offerte_E_MLIBERO_{day:%Y%m%d}.xml"


def file_url(day: date) -> str:
    """The address of the daily file of `day`.

    The folder of a month does not pad it (2026_3), the file name does
    (20260305): a padded folder answers 404.
    """
    return f"{PORTALE}/{day.year}_{day.month}/{file_name(day)}"


# --- Conversion -----------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Turn the values of an OfferInfo into JSON ones."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def info_dict(info: OfferInfo) -> dict[str, Any]:
    """The offer information, as stored."""
    return {
        name: _plain(getattr(info, name)) for name in info.__slots__ if name != "code"
    }


def entry(conversion: Conversion) -> Entry:
    """What is stored for one offer, apart from the dates it was seen."""
    contract = conversion.contract
    return {
        "converter": CONVERTER,
        "info": info_dict(conversion.info),
        "status": conversion.status.value,
        "problems": list(conversion.problems),
        "warnings": list(conversion.warnings),
        "contract": None if contract is None else contract.to_dict(),
    }


def read_day(data: bytes) -> dict[str, Entry]:
    """Convert the household offers of one daily file, by code."""
    offers: dict[str, Entry] = {}
    try:
        for offer in parse_offers(io.BytesIO(data)):
            if offer.info.domestic and offer.info.code:
                offers[offer.info.code] = entry(convert(offer))
    except ET.ParseError as err:
        raise UpdateError(f"the file is not valid XML: {err}") from err
    if not offers:
        raise UpdateError("the file holds no offer for households")
    return offers


# --- Merging -------------------------------------------------------------------


def on_sale(data: dict[str, Any] | None) -> set[str]:
    """The codes on sale on the day `data` was updated."""
    if data is None:
        return set()
    return {code for code, item in data["offers"].items() if item["last_seen"] is None}


def merged(
    old: dict[str, Any] | None, day: date, fresh: dict[str, Entry]
) -> dict[str, Any]:
    """Merge the offers of `day` into the file; `old` is not modified."""
    offers: dict[str, Entry] = {
        code: dict(item) for code, item in (old["offers"] if old else {}).items()
    }
    updated = date.fromisoformat(old["updated"]) if old else None
    forward = updated is None or day >= updated
    iso = day.isoformat()

    for code, content in fresh.items():
        item = offers.get(code)
        if item is None:
            offers[code] = {
                "first_seen": iso,
                "last_seen": None if forward else iso,
                **content,
            }
            continue
        item["first_seen"] = min(item["first_seen"], iso)
        if forward:
            item["last_seen"] = None
            item.update(content)
        elif item["last_seen"] is not None and iso >= item["last_seen"]:
            item["last_seen"] = iso
            item.update(content)

    if forward and updated is not None and day > updated:
        for code in on_sale(old) - fresh.keys():
            offers[code]["last_seen"] = updated.isoformat()

    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "updated": iso if forward else old["updated"],
        "file": file_name(day) if forward else old["file"],
        "offers": dict(sorted(offers.items())),
    }


def changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Describe what changed, one line per offer."""
    before = old["offers"] if old else {}
    lines: list[str] = []
    for code, item in new["offers"].items():
        name = item["info"]["name"]
        previous = before.get(code)
        if previous is None:
            lines.append(f"added {code} {name}")
        elif previous["last_seen"] is None and item["last_seen"] is not None:
            lines.append(f"withdrawn {code} {name}")
        elif previous != item:
            lines.append(f"changed {code} {name}")
    return lines


def check_share(old: dict[str, Any] | None, fresh: dict[str, Entry]) -> None:
    """Refuse a file that lost too many of the offers on sale."""
    current = on_sale(old)
    if current and len(fresh) < MIN_SHARE * len(current):
        raise UpdateError(
            f"the file holds {len(fresh)} of {len(current)} offers on sale: "
            "probably truncated"
        )


# --- Command line -------------------------------------------------------------


def dumps(data: dict[str, Any]) -> str:
    """Write the file with one offer per line, so a commit shows what changed."""
    head = [
        f"  {json.dumps(key)}: {json.dumps(data[key], ensure_ascii=False)},"
        for key in ("schema_version", "source", "updated", "file")
    ]
    body = [
        f"    {json.dumps(code)}: "
        f"{json.dumps(item, ensure_ascii=False, separators=(',', ':'))}"
        for code, item in data["offers"].items()
    ]
    return "\n".join(["{", *head, '  "offers": {', ",\n".join(body), "  }", "}"]) + "\n"


def load(path: Path) -> dict[str, Any] | None:
    """Read the file, or None when it does not exist yet."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise UpdateError(f"{path}: unsupported schema {data.get('schema_version')!r}")
    return data


def latest(get: Fetch, today: date) -> tuple[date, bytes]:
    """The most recent daily file, looking back a few days."""
    for back in range(LOOK_BACK_DAYS + 1):
        day = today - timedelta(days=back)
        try:
            return day, get(file_url(day))
        except NotFoundError:
            continue
    raise UpdateError(
        f"no daily file from {today - timedelta(days=LOOK_BACK_DAYS)} to {today}"
    )


def daily(get: Fetch, old: dict[str, Any] | None, today: date) -> dict[str, Any]:
    """Merge the most recent daily file."""
    day, data = latest(get, today)
    try:
        fresh = read_day(data)
    except UpdateError as err:
        raise UpdateError(f"{file_name(day)}: {err}") from err
    check_share(old, fresh)
    return merged(old, day, fresh)


def backfill(
    get: Fetch, old: dict[str, Any] | None, first: date, last: date
) -> dict[str, Any]:
    """Merge every daily file from `first` to `last`, in order."""
    data = old
    missing: list[str] = []
    unreadable: list[str] = []
    day = first
    while day <= last:
        try:
            raw = get(file_url(day))
        except NotFoundError:
            missing.append(day.isoformat())
        else:
            try:
                data = merged(data, day, read_day(raw))
            except UpdateError as err:
                unreadable.append(f"{day} ({err})")
        day += timedelta(days=1)
    if missing:
        print(f"Missing: {', '.join(missing)}", file=sys.stderr)
    if unreadable:
        print(f"Unreadable: {', '.join(unreadable)}", file=sys.stderr)
    if data is None:
        raise UpdateError(f"no daily file from {first} to {last}")
    return data


def date_range(text: str) -> tuple[date, date]:
    """Read FROM:TO."""
    first, _, last = text.partition(":")
    start, end = date.fromisoformat(first), date.fromisoformat(last or first)
    if end < start:
        raise argparse.ArgumentTypeError(f"{text}: the range ends before it starts")
    return start, end


def main(argv: list[str] | None = None, get: Fetch = fetch) -> int:
    """Update the file; see the module docstring for the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--backfill", type=date_range, metavar="FROM:TO")
    args = parser.parse_args(argv)

    try:
        old = load(args.output)
        if args.backfill:
            new = backfill(get, old, *args.backfill)
        else:
            new = daily(get, old, args.today)
    except (UpdateError, ValueError) as err:
        print(f"Not updated: {err}", file=sys.stderr)
        return 1

    lines = changes(old, new)
    if old is not None and new == old:
        print("No change.")
        return 0
    args.output.write_text(dumps(new), encoding="utf-8")
    print("\n".join(lines) if lines else "Updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
