# SPDX-License-Identifier: GPL-3.0-or-later
"""GME API messages. Run as a script: python3 tests/test_gme.py

The responses are built here, in the shape the GME technical manual
describes; every price is synthetic and nothing is downloaded.
"""

import base64
import io
import json
import sys
import zipfile
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.gme import (
    ATTRIBUTION,
    AUTH_URL,
    DATA_URL,
    QUOTAS_URL,
    GmeAuthError,
    GmeError,
    auth_body,
    pun_prices,
    pun_request,
    token_from,
)
from engine.pun import monthly_averages, slot_start

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


def record(
    day: str, hour: int, zone: str, price: object, period: str | None = None
) -> dict[str, object]:
    """One row of ME_ZonalPrices."""
    return {
        "FlowDate": int(day),
        "Hour": hour,
        "Period": period if period is not None else str(hour),
        "Market": "MGP",
        "Zone": zone,
        "Price": price,
        "Notes": "",
    }


def packed(content: bytes, name: str = "ME_ZonalPrices.json") -> str:
    """Zip `content` as the only file and encode it in base64."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


def response(rows: list[dict[str, object]]) -> dict[str, object]:
    """A successful RequestData response carrying `rows`."""
    return {
        "RequestId": "synthetic",
        "FormatType": ".json.zip",
        "ResultRequest": "",
        "ContentResponse": packed(json.dumps(rows).encode()),
    }


def test_urls() -> None:
    """The endpoints and the attribution the terms of use ask for."""
    check("auth URL", AUTH_URL, "https://api.mercatoelettrico.org/request/api/v1/Auth")
    check(
        "data URL",
        DATA_URL,
        "https://api.mercatoelettrico.org/request/api/v1/RequestData",
    )
    check(
        "quotas URL",
        QUOTAS_URL,
        "https://api.mercatoelettrico.org/request/api/v1/GetMyQuotas",
    )
    check("attribution", ATTRIBUTION, "Source: Gestore dei Mercati Energetici S.p.A.")


def test_auth() -> None:
    """The login is sent as GME asks; the token comes back or the reason why not."""
    check(
        "auth body",
        auth_body("user", "secret"),
        {"Login": "user", "Password": "secret"},
    )
    check(
        "token",
        token_from({"Success": True, "token": "jwt", "Reason": ""}),
        "jwt",
    )
    raises(
        "wrong password",
        GmeAuthError,
        token_from,
        {"Success": False, "token": "", "Reason": "Invalid credentials"},
    )
    raises(
        "success without a token",
        GmeAuthError,
        token_from,
        {"Success": True, "token": ""},
    )
    raises("not an object", GmeAuthError, token_from, ["jwt"])
    check("an auth error is a GME error", issubclass(GmeAuthError, GmeError), True)


def test_request() -> None:
    """PUN prices are zonal prices of the day-ahead market, hourly by default."""
    check(
        "hourly request",
        pun_request(date(2026, 11, 1), date(2026, 11, 12)),
        {
            "Platform": "PublicMarketResults",
            "Segment": "MGP",
            "DataName": "ME_ZonalPrices",
            "IntervalStart": 20261101,
            "IntervalEnd": 20261112,
            "Attributes": {"GranularityType": "PT60"},
        },
    )
    check(
        "quarter-hour request",
        pun_request(date(2026, 11, 1), date(2026, 11, 1), 15)["Attributes"],
        {"GranularityType": "PT15"},
    )
    raises(
        "end before start",
        GmeError,
        pun_request,
        date(2026, 11, 2),
        date(2026, 11, 1),
    )
    raises(
        "slots of 30 minutes",
        GmeError,
        pun_request,
        date(2026, 11, 1),
        date(2026, 11, 1),
        30,
    )


def test_prices() -> None:
    """Only the PUN rows are kept, each at the start of its slot."""
    rows = [
        record("20261025", 2, "NORD", 90.5),
        record("20261025", 3, "PUN", 101.25),
        record("20261025", 4, "PUN", 99),
        record("20261025", 25, "PUN", "120.123456"),
        record("20261025", 5, "PUN", None),  # auction cancelled
        record("20261025", 3, "SUD", 80),
    ]
    prices = pun_prices(response(rows))
    autumn = date(2026, 10, 25)
    check(
        "PUN rows only, cancelled ones dropped",
        prices,
        [
            (slot_start(autumn, 3), D("101.25")),
            (slot_start(autumn, 4), D(99)),
            (slot_start(autumn, 25), D("120.123456")),
        ],
    )
    check(
        "the two 02:00 are different moments",
        prices[0][0].utcoffset() != prices[1][0].utcoffset(),
        True,
    )
    check("prices are exact decimals", type(prices[0][1]), Decimal)

    quarters = [
        record("20261112", 9, "PUN", 110, period="33"),
        record("20261112", 9, "PUN", 111, period="34"),
    ]
    day = date(2026, 11, 12)
    check(
        "quarter-hour rows use the period",
        pun_prices(response(quarters), 15),
        [(slot_start(day, 33, 15), D(110)), (slot_start(day, 34, 15), D(111))],
    )
    check("an empty answer has no prices", pun_prices(response([])), [])


def test_month() -> None:
    """The rows of a whole month feed the monthly averages."""
    rows = []
    day = date(2026, 11, 1)
    while day.month == 11:
        for hour in range(1, 25):
            rows.append(record(f"{day:%Y%m%d}", hour, "PUN", 100 + hour))
            rows.append(record(f"{day:%Y%m%d}", hour, "NORD", 1))
        day = date.fromordinal(day.toordinal() + 1)
    averages = monthly_averages(date(2026, 11, 1), pun_prices(response(rows)))
    check("a month of rows is complete", averages.complete, True)
    check("F0 is the mean of 101..124", averages.f0, D("112.50"))


def test_failures() -> None:
    """A failed request or a malformed answer is a GmeError with the reason."""
    failed = {
        "RequestId": "x",
        "FormatType": "",
        "ResultRequest": "Quota exceeded",
        "ContentResponse": "",
    }
    raises("failed request", GmeError, pun_prices, failed)
    for what, content in (
        ("not base64", "%%%"),
        ("not a zip", base64.b64encode(b"plain text").decode()),
        ("no json in the zip", packed(b"{}", name="readme.txt")),
        ("not json", packed(b"not json")),
        ("not a list", packed(b'{"Zone": "PUN"}')),
        ("row without a price", packed(json.dumps([{"Zone": "PUN"}]).encode())),
        (
            "hour that does not exist",
            packed(json.dumps([record("20261112", 25, "PUN", 1)]).encode()),
        ),
        (
            "date that does not exist",
            packed(json.dumps([record("20261131", 1, "PUN", 1)]).encode()),
        ),
    ):
        broken = {**response([]), "ContentResponse": content}
        raises(what, GmeError, pun_prices, broken)


def main() -> int:
    """Run every case and report."""
    for test in (
        test_urls,
        test_auth,
        test_request,
        test_prices,
        test_month,
        test_failures,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
