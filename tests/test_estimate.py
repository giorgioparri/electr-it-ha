# SPDX-License-Identifier: GPL-3.0-or-later
"""Running estimates. Run as a script: python3 tests/test_estimate.py

The expected unit prices are worked out by hand from the same bills used in
test_bills.py, so the two files check each other.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.calculator import CalculationError, Supply
from engine.contract import Contract
from engine.estimate import Market, fixed_cost_per_day, month_to_date, unit_price

D = Decimal
_failures: list[str] = []

AJO = Contract.from_dict(
    {
        "schema_version": 1,
        "supplier": "Ajo Energia",
        "price_type": "indexed",
        "time_bands": "f1_f2_f3",
        "index": {
            "name": "pun_index_gme",
            "period": "monthly",
            "coefficient": D("1.10"),
        },
        "dispatching": {
            "mode": "regulated",
            "fixed_value": None,
            "capacity_market": True,
        },
        "disp_bt": True,
        "fixed_fee_eur_year": 144,
    }
)
AJO_SUPPLY = Supply(resident=True, power_kw=D(6))
AJO_SEPTEMBER = Market(
    pun={"f1": D("0.206350"), "f2": D("0.227580"), "f3": D("0.197020")},
    dispatching=D("0.010501"),
    capacity=D("0.003197"),
)
AJO_KWH = {"f1": 21, "f2": 98, "f3": 372}

ENEL = Contract.from_dict(
    {
        "schema_version": 1,
        "supplier": "Enel Energia",
        "price_type": "indexed",
        "time_bands": "f1_f2_f3",
        "index": {
            "name": "pun_index_gme",
            "period": "monthly",
            "coefficient": D("1.10"),
            "loss_method": "price",
        },
        "energy_price": {"f1": D("0.0286"), "f2": D("0.0286"), "f3": D("0.011")},
        "dispatching": {
            "mode": "cdispd",
            "fixed_value": None,
            "capacity_market": False,
        },
        "disp_bt": True,
        "fixed_fee_eur_year": 144,
    }
)
# July 2026 GME monthly averages by band (source: Gestore dei Mercati
# Energetici S.p.A.); the bill shows them multiplied by 1.10.
ENEL_JULY = Market(
    pun={"f1": D("0.15420"), "f2": D("0.16938"), "f3": D("0.15226")},
    cdispd=D("0.025043"),
)

SEN = Contract.from_dict(
    {
        "schema_version": 1,
        "supplier": "Servizio Elettrico Nazionale",
        "price_type": "regulated",
        "time_bands": "f1_f23",
        "dispatching": {
            "mode": "tutela",
            "fixed_value": None,
            "capacity_market": False,
        },
    }
)
SEN_SUPPLY = Supply(resident=True, power_kw=D(3))


def check(what: str, actual: Decimal, expected: str, tolerance: str = "0") -> None:
    """Compare a value with the expected one."""
    ok = abs(actual - D(expected)) <= D(tolerance)
    note = f" (tolerance {tolerance})" if tolerance != "0" else ""
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}{note}")
    if not ok:
        _failures.append(what)


def test_unit_price_ajo() -> None:
    """Ajo, September 2026: PUN x 1.10, dispatching and capacity x 1.10."""
    day = date(2026, 9, 15)
    price = unit_price(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, "f3")
    # 0.197020 x 1.10 + (0.010501 + 0.003197) x 1.10
    check("Ajo F3 sale", price.sale, "0.2317898")
    check("Ajo F3 network", price.network, "0.014730")
    check("Ajo F3 system charges", price.system_charges, "0.033153")
    check("Ajo F3 excise", price.excise, "0.0227")
    check("Ajo F3 total", price.total, "0.33261008")
    f1 = unit_price(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, "f1")
    f2 = unit_price(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, "f2")
    check("Ajo F1 total", f1.total, "0.34389938")
    check("Ajo F2 total", f2.total, "0.36958768")


def test_unit_price_enel() -> None:
    """Enel Flex, July 2026: PUN x 1.10 + alfa + CDispD."""
    day = date(2026, 7, 15)
    supply = Supply(resident=True, power_kw=D(5))
    # f1: (0.15420 x 1.10 + 0.0286 + 0.025043 + 0.070583) x 1.10
    for band, expected in (
        ("f1", "0.3232306"),
        ("f2", "0.3415984"),
        ("f3", "0.3015232"),
    ):
        price = unit_price(ENEL, supply, ENEL_JULY, day, band)
        check(f"Enel {band} total", price.total, expected)


def test_unit_price_tutela() -> None:
    """Regulated service: before taxes it equals the ARERA published total."""
    day = date(2026, 8, 15)
    price = unit_price(SEN, SEN_SUPPLY, Market(), day, "f23", month_kwh=D(100))
    check("SEN F23 before taxes", price.taxable, "0.221983")
    check("SEN F23 total, inside the allowance", price.total, "0.2441813")


def test_excise_allowance() -> None:
    """Resident, 3 kW: the next kWh is exempt, taxed, or taxed twice."""
    day = date(2026, 8, 15)
    for month_kwh, expected in (
        (0, "0"),
        (149, "0"),
        (150, "0.0227"),
        (219, "0.0227"),
        (220, "0.0454"),
        (369, "0.0454"),
        (370, "0.0227"),
    ):
        price = unit_price(SEN, SEN_SUPPLY, Market(), day, "f1", month_kwh=D(month_kwh))
        check(f"excise on the next kWh after {month_kwh}", price.excise, expected)


def test_fixed_cost() -> None:
    """Ajo, 6 kW: 28.4588 EUR per month with VAT, spread over the days."""
    # (12.00 + 0.09225 + 1.92 + 6 x (1.96 + 0.1988 / 12)) x 1.10 / 30
    september = fixed_cost_per_day(AJO, AJO_SUPPLY, date(2026, 9, 10))
    check("Ajo fixed cost per day, September", september, "0.948627", "0.000001")
    check("Ajo fixed cost for September", september * 30, "28.4588", "0.0001")
    october = fixed_cost_per_day(AJO, AJO_SUPPLY, date(2026, 10, 10))
    check("Ajo fixed cost for October", october * 31, "28.4588", "0.0001")


def test_month_to_date() -> None:
    """The month so far is a bill that ends today."""
    whole = month_to_date(AJO, AJO_SUPPLY, AJO_SEPTEMBER, date(2026, 9, 30), AJO_KWH)
    check("Ajo whole September, total", whole.total, "195.61")
    half = month_to_date(AJO, AJO_SUPPLY, AJO_SEPTEMBER, date(2026, 9, 15), AJO_KWH)
    check("months billed on the 15th", half.line("network_fixed").quantity, "0.5")
    check("metering charge on the 15th", half.line("network_fixed").amount, "0.96")
    check("fixed fee on the 15th", half.line("fixed_fee").amount, "6.00")


def test_unit_price_matches_bill() -> None:
    """1000 kWh more on the bill cost 1000 times the unit price."""
    day = date(2026, 9, 30)
    more = dict(AJO_KWH, f3=AJO_KWH["f3"] + 1000)
    base = month_to_date(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, AJO_KWH)
    bigger = month_to_date(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, more)
    per_kwh = (bigger.total - base.total) / 1000
    price = unit_price(AJO, AJO_SUPPLY, AJO_SEPTEMBER, day, "f3")
    check("bill difference per kWh", per_kwh, str(price.total), "0.0001")


def test_missing_data() -> None:
    """Missing market values and wrong bands are errors, never guesses."""
    day = date(2026, 9, 15)
    for what, market, band in (
        ("no PUN", Market(dispatching=D("0.01"), capacity=D("0.003")), "f1"),
        ("no dispatching", Market(pun=AJO_SEPTEMBER.pun, capacity=D("0.003")), "f1"),
        ("band not in the contract", AJO_SEPTEMBER, "f23"),
    ):
        try:
            unit_price(AJO, AJO_SUPPLY, market, day, band)
        except CalculationError:
            raised = D(1)
        else:
            raised = D(0)
        check(f"error raised: {what}", raised, "1")


def main() -> int:
    """Run every case and report."""
    for test in (
        test_unit_price_ajo,
        test_unit_price_enel,
        test_unit_price_tutela,
        test_excise_allowance,
        test_fixed_cost,
        test_month_to_date,
        test_unit_price_matches_bill,
        test_missing_data,
    ):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
