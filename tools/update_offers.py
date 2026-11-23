#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Update the offers/ folder from the open data of the Portale Offerte.

Run by a scheduled workflow, not by Home Assistant. Every day the Portale
Offerte (ARERA, Acquirente Unico) publishes the electricity offers of the free
market as one XML file, kept in a folder per month. This reads the file of the
day, converts every offer for households with engine/offers.py, and merges
the result into the offers/ folder, laid out as engine/catalogue.py reads it:
index.json, plus one file per first two hex digits of the hash of the code.

- an offer keeps the date it was first seen; while it is on sale its
  "last_seen" is null, and once it leaves the Portale it records the last day
  it was there. Withdrawn offers stay, for the bills that still carry their
  code;
- the content of an offer is the one of the most recent day it was seen.

The daily update works one file of the folder at a time, so that its memory
stays low on a small machine, and writes only the files that changed, the
index last. A run stopped half way leaves the index of the day before, and
running the same day again completes it.

With --backfill, the daily files of a range of days are read in order, to
fill the archive once; days with no file, or with a file that cannot be read
(the Portale serves some past days as an empty file), are listed and skipped.
A backfill holds the whole archive in memory: run it on a desktop, not on
the small machine of the daily update.

A missing index.json stops both, unless --init says to start a new archive:
an index lost by mistake must not turn the archive into the offers of one day.

With --convert, the offers.json of schema 1 (one file for every offer) is
split into a new folder once.

Exit status: 0 when the folder is up to date (written or unchanged); 1 when
nothing could be read, or what was read cannot be trusted: nothing is written.

Usage:
    python3 tools/update_offers.py [--output DIR] [--today YYYY-MM-DD] [--init]
    python3 tools/update_offers.py [--output DIR] --backfill FROM:TO [--init]
    python3 tools/update_offers.py [--output DIR] --convert offers.json

Standard library only.
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "custom_components" / "electr_it_ha"))

from engine.catalogue import (  # noqa: E402
    HASH,
    INDEX_FILE,
    SCHEMA_VERSION,
    CatalogueIndex,
    shard_key,
)
from engine.offers import Conversion, OfferInfo, convert, parse_offers  # noqa: E402

OUTPUT = Path("offers")
# Hex digits of the hash that name a file: 256 files of about 300 KB each.
DIGITS = 2
# The schema of the single offers.json that --convert reads.
OLD_SCHEMA_VERSION = 1
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


def check_share(on_sale_before: int, fresh: dict[str, Entry]) -> None:
    """Refuse a file that lost too many of the offers on sale."""
    if on_sale_before and len(fresh) < MIN_SHARE * on_sale_before:
        raise UpdateError(
            f"the file holds {len(fresh)} of {on_sale_before} offers on sale: "
            "probably truncated"
        )


# --- The folder ---------------------------------------------------------------


def shard_name(code: str) -> str:
    """The file of the folder that holds `code`."""
    return f"{shard_key(code, DIGITS)}.json"


def dumps_shard(offers: dict[str, Entry]) -> str:
    """Write one file of offers, one offer per line, so a commit shows what changed."""
    body = [
        f"    {json.dumps(code)}: "
        f"{json.dumps(item, ensure_ascii=False, separators=(',', ':'))}"
        for code, item in sorted(offers.items())
    ]
    head = ["{", f'  "schema_version": {SCHEMA_VERSION},', '  "offers": {']
    return "\n".join([*head, ",\n".join(body), "  }", "}"]) + "\n"


def dumps_index(updated: str, file: str, offers: int, on_sale: int) -> str:
    """Write index.json."""
    index = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "updated": updated,
        "file": file,
        "shards": {"hash": HASH, "digits": DIGITS},
        "offers": offers,
        "on_sale": on_sale,
    }
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def files(data: dict[str, Any]) -> dict[str, str]:
    """The text of every file of the folder holding `data`, by name."""
    shards: dict[str, dict[str, Entry]] = defaultdict(dict)
    for code, item in data["offers"].items():
        shards[shard_name(code)][code] = item
    texts = {name: dumps_shard(offers) for name, offers in sorted(shards.items())}
    texts[INDEX_FILE] = dumps_index(
        data["updated"], data["file"], len(data["offers"]), len(on_sale(data))
    )
    return texts


def read_index(folder: Path, init: bool) -> dict[str, Any] | None:
    """index.json of the folder; None when it is missing and --init allows it."""
    path = folder / INDEX_FILE
    if not path.exists():
        if init:
            return None
        raise UpdateError(f"{path} not found: use --init to start a new archive")
    data = json.loads(path.read_text(encoding="utf-8"))
    index = CatalogueIndex.from_dict(data)
    if index.digits != DIGITS:
        raise UpdateError(f"{path}: {index.digits} digits, this writes {DIGITS}")
    return data


def read_shard(path: Path) -> dict[str, Entry]:
    """The offers of one file of the folder."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise UpdateError(f"{path}: unsupported schema {data.get('schema_version')!r}")
    return data["offers"]


def shard_names(folder: Path) -> set[str]:
    """The files of offers already in the folder."""
    if not folder.exists():
        return set()
    return {path.name for path in folder.glob("*.json") if path.name != INDEX_FILE}


def read_folder(folder: Path, init: bool) -> dict[str, Any] | None:
    """The whole archive in memory, as merged() works on it."""
    index = read_index(folder, init)
    if index is None:
        return None
    offers: dict[str, Entry] = {}
    for name in sorted(shard_names(folder)):
        offers.update(read_shard(folder / name))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "updated": index["updated"],
        "file": index["file"],
        "offers": offers,
    }


def write(folder: Path, name: str, text: str) -> bool:
    """Write one file unless it already holds `text`; True when written.

    Through a temporary file, so a stopped run never leaves half a file.
    """
    path = folder / name
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    folder.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return True


def write_all(folder: Path, texts: dict[str, str]) -> bool:
    """Write the files that changed, the index last; True when any was written."""
    names = [*sorted(name for name in texts if name != INDEX_FILE), INDEX_FILE]
    results = [write(folder, name, texts[name]) for name in names]
    return any(results)


# --- Command line -------------------------------------------------------------


def by_code(lines: list[str]) -> list[str]:
    """Sort the lines of changes() by code, whichever file they came from."""
    return sorted(lines, key=lambda line: line.split(" ", 2)[1])


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


def daily(get: Fetch, folder: Path, today: date, init: bool) -> tuple[bool, list[str]]:
    """Merge the most recent daily file, one file of the folder at a time.

    Everything that can refuse the day is checked before the first write.
    Return whether anything was written, and the changes.
    """
    index = read_index(folder, init)
    day, data = latest(get, today)
    try:
        fresh = read_day(data)
    except UpdateError as err:
        raise UpdateError(f"{file_name(day)}: {err}") from err
    check_share(index["on_sale"] if index else 0, fresh)

    groups: dict[str, dict[str, Entry]] = defaultdict(dict)
    for code, content in fresh.items():
        groups[shard_name(code)][code] = content

    lines: list[str] = []
    written = False
    total = selling = 0
    updated = file = ""
    for name in sorted(shard_names(folder) | groups.keys()):
        path = folder / name
        old = None
        if index is not None:
            old = {
                "updated": index["updated"],
                "file": index["file"],
                "offers": read_shard(path) if path.exists() else {},
            }
        new = merged(old, day, groups.get(name, {}))
        lines += changes(old, new)
        written |= write(folder, name, dumps_shard(new["offers"]))
        total += len(new["offers"])
        selling += len(on_sale(new))
        updated, file = new["updated"], new["file"]
    written |= write(folder, INDEX_FILE, dumps_index(updated, file, total, selling))
    return written, by_code(lines)


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


def convert_old(path: Path, folder: Path) -> tuple[int, int]:
    """Split the offers.json of schema 1 into a new folder.

    Return the number of offers and of files written.
    """
    if (folder / INDEX_FILE).exists():
        raise UpdateError(f"{folder / INDEX_FILE} exists: convert into a new folder")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != OLD_SCHEMA_VERSION:
        raise UpdateError(
            f"{path}: unsupported schema {data.get('schema_version')!r} "
            f"(expected {OLD_SCHEMA_VERSION})"
        )
    texts = files(data)
    write_all(folder, texts)
    return len(data["offers"]), len(texts)


def date_range(text: str) -> tuple[date, date]:
    """Read FROM:TO."""
    first, _, last = text.partition(":")
    start, end = date.fromisoformat(first), date.fromisoformat(last or first)
    if end < start:
        raise argparse.ArgumentTypeError(f"{text}: the range ends before it starts")
    return start, end


def main(argv: list[str] | None = None, get: Fetch = fetch) -> int:
    """Update the folder; see the module docstring for the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--init", action="store_true", help="start a new archive if none exists"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill", type=date_range, metavar="FROM:TO")
    mode.add_argument("--convert", type=Path, metavar="OFFERS_JSON")
    args = parser.parse_args(argv)
    folder: Path = args.output

    try:
        if args.convert:
            offers, count = convert_old(args.convert, folder)
            print(f"Converted {offers} offers into {count} files.")
            return 0
        if args.backfill:
            old = read_folder(folder, args.init)
            new = backfill(get, old, *args.backfill)
            lines = changes(old, new)
            written = write_all(folder, files(new))
        else:
            written, lines = daily(get, folder, args.today, args.init)
    except (UpdateError, ValueError) as err:
        print(f"Not updated: {err}", file=sys.stderr)
        return 1

    if not written:
        print("No change.")
    else:
        print("\n".join(lines) if lines else "Updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
