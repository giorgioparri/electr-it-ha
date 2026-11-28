#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Update regulated_values.json from the parameters file of the Portale Offerte.

Run by a scheduled workflow, not by Home Assistant. The Portale Offerte
publishes once a day, in its open data, the parameters it uses for the yearly
cost of every offer (the "Parametri (csv)" link): one CSV per day, with the
columns nome_parametro, valore, descrizione. Thirteen of them are the
regulated values of engine.regulated for household customers; they go to the
archive kept by engine.published_rates, which gives each value to the whole
quarter of the day of the file.

The Portale's archive has gaps (whole months answer 404), so the raw file is
also kept, in a folder of its own, whenever its content changes.

Exit status:

- 0: the archive is up to date (written or unchanged);
- 1: no file could be read, or what it says does not fit the contract model:
  nothing is written;
- 3: the archive was written, but the values of the running quarter look
  missing or not updated yet.

Usage: python3 tools/update_portale_parameters.py [--output PATH]
       [--archive FOLDER] [--today YYYY-MM-DD]

Standard library only.
"""

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "custom_components" / "electr_it_ha"))

from engine.published_rates import (  # noqa: E402
    PublishedRates,
    PublishedRatesError,
    quarter_bounds,
)
from engine.regulated import Component  # noqa: E402

OUTPUT = Path("regulated_values.json")
ARCHIVE = Path("portale-parameters")
PORTALE = (
    "https://www.ilportaleofferte.it/portaleOfferte/resources/opendata/csv/parametriML"
)
USER_AGENT = (
    "electrITHA regulated values (+https://github.com/giorgioparri/electr-it-ha)"
)
TIMEOUT_SECONDS = 60
HEADER = ["nome_parametro", "valore", "descrizione"]

# Days before today to look back to when today's file is not out yet.
LOOK_BACK_DAYS = 3
# From this day of a quarter, its values should be there and updated; after
# the last one, a quarter whose values did not change is taken as it is.
GRACE_DAYS = 5
WATCH_DAYS = 20

_C = Component

# Parameter of the Portale -> component of the built-in table. Same units:
# EUR/kWh, EUR/year per POD, EUR/year per kW, VAT as a fraction.
MAPPING: dict[str, Component] = {
    "sigma1": _C.NETWORK_FIXED_EUR_POD_YEAR,
    "sigma2": _C.NETWORK_POWER_EUR_KW_YEAR,
    "sigma3": _C.NETWORK_ENERGY_EUR_KWH,
    "uc3": _C.UC3_EUR_KWH,
    "uc6p_d": _C.UC6_EUR_KWH,
    "uc6s_d": _C.UC6_POWER_EUR_KW_YEAR,
    "asos_dr": _C.ASOS_EUR_KWH,
    "arim_dr": _C.ARIM_EUR_KWH,
    "asos_dnr_f": _C.ASOS_NON_RESIDENT_EUR_POD_YEAR,
    "dispbt_d": _C.DISP_BT_EUR_POD_YEAR,
    "pcv_c": _C.TUTELA_PCV_EUR_POD_YEAR,
    "acc_c_nr": _C.EXCISE_EUR_KWH,
    "iva_c": _C.VAT_RATE,
}

# What the contract model takes for granted. If the Portale ever disagrees,
# the model no longer fits and nothing is written.
SAME_AS: dict[str, str] = {
    # One excise rate for every household supply: the allowance for
    # residents up to 3 kW is in the calculator, not in the rate.
    "acc_c_r_h": "acc_c_nr",
    "acc_c_r_l": "acc_c_nr",
    # Non-residents pay the same per-kWh charges as residents.
    "asos_dnr_v": "asos_dr",
    "arim_dnr_v": "arim_dr",
}
FIXED: dict[str, Decimal] = {
    # No fixed ARIM quota for non-residents.
    "arim_dnr_f": Decimal(0),
    # The calculator's STANDARD_LOSS_FACTOR.
    "lambda": Decimal("0.1"),
}

type Fetch = Callable[[str], bytes]


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
            raise NotFoundError(f"{url}: not published") from err
        raise UpdateError(f"cannot download {url}: {err}") from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise UpdateError(f"cannot download {url}: {err}") from err


def file_name(day: date) -> str:
    """Return the name of the parameters file of `day`."""
    return f"PO_Parametri_Mercato_Libero_E_{day:%Y%m%d}.csv"


def file_url(day: date) -> str:
    """Return the address of the parameters file of `day`."""
    return f"{PORTALE}/{day:%Y_%m}/{file_name(day)}"


def parse(data: bytes) -> dict[str, Decimal]:
    """Read the CSV: parameter name -> value."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise UpdateError(f"not UTF-8: {err}") from err
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [cell.strip() for cell in rows[0]] != HEADER:
        raise UpdateError(f"the first row is not {','.join(HEADER)}")
    params: dict[str, Decimal] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(HEADER):
            raise UpdateError(f"row {number}: expected {len(HEADER)} columns")
        name, value = row[0].strip(), row[1].strip()
        if name in params:
            raise UpdateError(f"row {number}: '{name}' appears twice")
        try:
            number_value = Decimal(value)
        except InvalidOperation:
            raise UpdateError(f"row {number}: '{value}' is not a number") from None
        if not number_value.is_finite():
            raise UpdateError(f"row {number}: '{value}' is not a finite number")
        params[name] = number_value
    if not params:
        raise UpdateError("the file has no parameters")
    return params


def regulated_values(params: Mapping[str, Decimal]) -> dict[Component, Decimal]:
    """Pick the regulated values, after checking what the model takes for granted."""
    needed = [*MAPPING, *SAME_AS, *SAME_AS.values(), *FIXED]
    missing = sorted({name for name in needed if name not in params})
    if missing:
        raise UpdateError(f"missing parameters: {', '.join(missing)}")
    problems = [
        f"{name} is {params[name]}, not {params[other]} as {other}"
        for name, other in SAME_AS.items()
        if params[name] != params[other]
    ]
    problems.extend(
        f"{name} is {params[name]}, not {expected}"
        for name, expected in FIXED.items()
        if params[name] != expected
    )
    problems.extend(
        f"{name} is negative: {params[name]}" for name in MAPPING if params[name] < 0
    )
    if problems:
        raise UpdateError("; ".join(problems))
    return {component: params[name].normalize() for name, component in MAPPING.items()}


def archive(folder: Path, day: date, data: bytes) -> Path | None:
    """Keep the raw file of `day` when it differs from the last one kept."""
    kept = sorted(folder.glob("PO_Parametri_*.csv")) if folder.is_dir() else []
    if kept and kept[-1].read_bytes() == data:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name(day)
    path.write_bytes(data)
    return path


def overdue(published: PublishedRates, today: date) -> list[str]:
    """Return why the values of the running quarter look missing or stale.

    Only from GRACE_DAYS to WATCH_DAYS after the quarter starts. A quarter
    whose values are all the same as the quarter before is reported too,
    although tariffs sometimes do stay the same: someone should look.
    """
    start, _ = quarter_bounds(today)
    elapsed = (today - start).days
    if not GRACE_DAYS <= elapsed <= WATCH_DAYS:
        return []
    quarter = f"{start:%Y}-Q{(start.month - 1) // 3 + 1}"
    current = {
        item.component: item.value
        for item in published.entries
        if item.valid_from == start
    }
    missing = [c.value for c in MAPPING.values() if c not in current]
    if missing:
        return [f"no values for {quarter}: {', '.join(missing)}"]
    previous = {c: published.previous(c, today) for c in MAPPING.values()}
    if all(previous[c] == current[c] for c in MAPPING.values()):
        return [
            f"every value of {quarter} is the same as in the quarter before: "
            "the Portale may not be updated yet"
        ]
    return []


def read_latest(get: Fetch, today: date) -> tuple[date, bytes]:
    """Download the file of today, or of the most recent day before it."""
    for back in range(LOOK_BACK_DAYS + 1):
        day = today - timedelta(days=back)
        try:
            return day, get(file_url(day))
        except NotFoundError:
            continue
    raise UpdateError(
        f"no parameters file from {today - timedelta(days=LOOK_BACK_DAYS)} to {today}"
    )


def changes(old: PublishedRates, new: PublishedRates) -> list[str]:
    """Describe the difference between two archives, one line per value."""
    before = {(item.component, item.valid_from): item for item in old.entries}
    lines = []
    for item in new.entries:
        previous = before.get((item.component, item.valid_from))
        if previous == item:
            continue
        period = f"{item.valid_from}..{item.valid_to}"
        if previous is None:
            lines.append(f"added {item.component} {period}: {item.value}")
        else:
            lines.append(
                f"changed {item.component} {period}: {previous.value} -> {item.value}"
            )
    return lines


def write(path: Path, published: PublishedRates) -> None:
    """Write the archive the way it is kept on the data branch."""
    text = json.dumps(published.to_dict(), indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None, get: Fetch = fetch) -> int:
    """Update the archive; see the module docstring for the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)

    try:
        old = PublishedRates.empty()
        if args.output.exists():
            old = PublishedRates.from_dict(json.loads(args.output.read_text("utf-8")))
        day, data = read_latest(get, args.today)
        values = regulated_values(parse(data))
        new = old.observe(day, values, file_name(day))
    except (UpdateError, PublishedRatesError, ValueError) as err:
        print(f"Not updated: {err}", file=sys.stderr)
        return 1

    print(f"Read {file_name(day)}.")
    kept = archive(args.archive, day, data)
    if kept is not None:
        print(f"Kept {kept.name}.")
    if new is old:
        print("No change.")
    else:
        write(args.output, new)
        print("\n".join(changes(old, new)))
    found = overdue(new, args.today)
    if found:
        print(f"Check: {'; '.join(found)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
