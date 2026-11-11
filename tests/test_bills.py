# SPDX-License-Identifier: GPL-3.0-or-later
"""Real bills recomputed by the engine. Run as a script: python3 tests/test_bills.py

Each case is a bill that was actually issued. Market values (PUN, dispatching,
capacity market, CDispD) are the ones printed on the bill; everything regulated
comes from engine.regulated.
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.calculator import Bill, Category, Period, Supply, compute_bill
from engine.contract import Contract

D = Decimal
_failures: list[str] = []


def check(
    case: str, what: str, actual: Decimal, expected: str, tolerance: str = "0"
) -> None:
    """Compare an amount with the bill, to the cent unless a tolerance is given."""
    ok = abs(actual - D(expected)) <= D(tolerance)
    note = f" (tolerance {tolerance})" if tolerance != "0" else ""
    print(
        f"{'PASS' if ok else 'FAIL'}  {case}: {what} = {actual}, bill says {expected}{note}"
    )
    if not ok:
        _failures.append(f"{case}: {what}")


def ajo_september_2026() -> Bill:
    """Ajo Energia: PUN Index GME by band, 10% losses, zero spread, 6 kW, resident.

    Losses are billed as extra kWh, rounded to the kWh in each band: the default.
    """
    contract = Contract.from_dict(
        {
            "schema_version": 1,
            "supplier": "Ajo Energia",
            "offer_name": "AJO_WEB_FASCE_0123",
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": {
                "name": "pun_index_gme",
                "period": "monthly",
                "coefficient": D("1.10"),
            },
            "energy_price": {"f1": 0, "f2": 0, "f3": 0},
            "dispatching": {
                "mode": "regulated",
                "fixed_value": None,
                "capacity_market": True,
            },
            "disp_bt": True,
            "fixed_fee_eur_year": 144,
        }
    )
    september = Period(
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
        kwh={"f1": 21, "f2": 98, "f3": 372},
        pun={"f1": D("0.206350"), "f2": D("0.227580"), "f3": D("0.197020")},
        dispatching=D("0.010501"),
        capacity=D("0.003197"),
    )
    return compute_bill(contract, Supply(resident=True, power_kw=D(6)), [september])


def test_ajo() -> None:
    """Full detail available: every total must match to the cent."""
    case = "Ajo 2026-09"
    bill = ajo_september_2026()
    check(case, "number of lines", D(len(bill.lines)), "19")
    check(case, "kWh billed for dispatching", bill.line("dispatching").quantity, "540")
    check(case, "kWh subject to excise", bill.line("excise").quantity, "491")
    check(case, "taxable", bill.taxable, "177.82")
    check(case, "VAT, rounded line by line", bill.vat, "17.79")
    check(case, "total", bill.total, "195.61")


def enel_june_july_2026() -> Bill:
    """Enel Energia, Enel Flex: PUN x 1.10 + alfa + CDispD, 5 kW, resident.

    The PUN below is the monthly average by band published by GME (source:
    Gestore dei Mercati Energetici S.p.A.). The bill shows it already
    multiplied by 1.10 for losses (0.138340 for June F1): Enel bills losses in
    the price, not as extra kWh.
    """
    contract = Contract.from_dict(
        {
            "schema_version": 1,
            "supplier": "Enel Energia",
            "offer_name": "Enel Flex",
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
    june = Period(
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
        kwh={"f1": 79, "f2": 60, "f3": 90},
        pun={"f1": D("0.12576"), "f2": D("0.15170"), "f3": D("0.12724")},
        cdispd=D("0.017139"),
    )
    july = Period(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        kwh={"f1": 53, "f2": 52, "f3": 67},
        pun={"f1": D("0.15420"), "f2": D("0.16938"), "f3": D("0.15226")},
        cdispd=D("0.025043"),
    )
    return compute_bill(contract, Supply(resident=True, power_kw=D(5)), [june, july])


def test_enel() -> None:
    """Two months with ASOS and DispBT changing on 1 July; summary rows only."""
    case = "Enel 2026-06/07"
    bill = enel_june_july_2026()
    check(case, "sale, energy", bill.subtotal(Category.SALE_ENERGY), "80.39")
    check(case, "sale, fixed", bill.subtotal(Category.SALE_FIXED), "24.19")
    check(case, "network", bill.subtotal(Category.NETWORK), "29.52")
    check(case, "ASOS", bill.amount("asos"), "11.98")
    check(case, "ARIM", bill.amount("arim"), "0.66")
    june_excise, july_excise = bill.lines_for("excise")
    check(case, "excise, June", june_excise.amount, "5.20")
    check(case, "excise, July", july_excise.amount, "3.90")
    check(case, "taxable", bill.taxable, "155.84")
    check(case, "VAT", bill.vat, "15.58")
    check(case, "total", bill.total, "171.42")


def sen_july_september_2026() -> Bill:
    """Servizio Elettrico Nazionale, regulated service, two bands, 3 kW, resident."""
    contract = Contract.from_dict(
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
    period = Period(
        start=date(2026, 7, 6), end=date(2026, 9, 5), kwh={"f1": 94, "f23": 184}
    )
    return compute_bill(contract, Supply(resident=True, power_kw=D(3)), [period])


def test_sen() -> None:
    """Partial: the bill has no detail lines, so rounding can differ by a cent.

    Excise and totals are not checked: the bill taxes 17 kWh out of 278 and
    the split that produces that figure is not on the summary.
    """
    case = "SEN 2026-07/09"
    bill = sen_july_september_2026()
    sale = bill.subtotal(Category.SALE_ENERGY) + bill.subtotal(Category.SALE_FIXED)
    check(
        case,
        "months billed for fixed charges",
        bill.line("network_fixed").quantity,
        "2",
    )
    check(case, "sale", sale, "54.10", tolerance="0.01")
    check(case, "network", bill.subtotal(Category.NETWORK), "19.79", tolerance="0.01")
    check(
        case,
        "system charges",
        bill.subtotal(Category.SYSTEM_CHARGES),
        "9.21",
        tolerance="0.01",
    )


def main() -> int:
    """Run every case and report."""
    for test in (test_ajo, test_enel, test_sen):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
