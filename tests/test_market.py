# SPDX-License-Identifier: GPL-3.0-or-later
"""Market values by name. Run as a script: python3 tests/test_market.py"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.calculator import CalculationError
from engine.contract import Contract
from engine.estimate import Market
from engine.market import build_market, required_market_values, to_eur_per_kwh

D = Decimal
_failures: list[str] = []


def contract(**fields: object) -> Contract:
    """Build a contract from the fields that matter for a case."""
    return Contract.from_dict({"schema_version": 1, "supplier": "Test", **fields})


AJO = contract(
    price_type="indexed",
    time_bands="f1_f2_f3",
    index={"name": "pun_index_gme", "period": "monthly", "coefficient": D("1.10")},
    dispatching={"mode": "regulated", "fixed_value": None, "capacity_market": True},
)
ENEL = contract(
    price_type="indexed",
    time_bands="f1_f2_f3",
    index={"name": "pun_index_gme", "period": "monthly", "coefficient": 1},
    dispatching={"mode": "cdispd", "fixed_value": None, "capacity_market": False},
)
INDEXED_TWO_BANDS = contract(
    price_type="indexed",
    time_bands="f1_f23",
    index={"name": "pun_index_gme", "period": "monthly", "coefficient": 1},
    dispatching={"mode": "included", "fixed_value": None, "capacity_market": False},
)
TUTELA = contract(
    price_type="regulated",
    time_bands="f1_f23",
    dispatching={"mode": "tutela", "fixed_value": None, "capacity_market": False},
)
FIXED_ALL_IN = contract(
    price_type="fixed",
    time_bands="mono",
    energy_price={"f0": D("0.15")},
    dispatching={"mode": "included", "fixed_value": None, "capacity_market": False},
)
FIXED_PASS_THROUGH = contract(
    price_type="fixed",
    time_bands="mono",
    energy_price={"f0": D("0.15")},
    dispatching={"mode": "regulated", "fixed_value": None, "capacity_market": True},
)


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual}, expected {expected}")
    if not ok:
        _failures.append(what)


def raises(what: str, call: object, *args: object) -> None:
    """Check that a call raises CalculationError."""
    try:
        call(*args)
    except CalculationError as err:
        print(f"PASS  {what}: {err}")
    else:
        print(f"FAIL  {what}: no error raised")
        _failures.append(what)


def test_required() -> None:
    """Each contract asks only for the values it is priced with."""
    for name, case, expected in (
        ("Ajo", AJO, ("pun_f1", "pun_f2", "pun_f3", "dispatching", "capacity")),
        ("Enel", ENEL, ("pun_f1", "pun_f2", "pun_f3", "cdispd")),
        ("indexed, two bands", INDEXED_TWO_BANDS, ("pun_f1", "pun_f23")),
        ("tutela", TUTELA, ()),
        ("fixed, all in", FIXED_ALL_IN, ()),
        ("fixed, pass-through", FIXED_PASS_THROUGH, ("dispatching", "capacity")),
    ):
        check(f"required for {name}", required_market_values(case), expected)


def test_build() -> None:
    """Values by name become the Market the contract needs."""
    values = {
        "pun_f1": D("0.206350"),
        "pun_f2": D("0.227580"),
        "pun_f3": D("0.197020"),
        "dispatching": D("0.010501"),
        "capacity": D("0.003197"),
        "cdispd": D("0.025043"),
    }
    pun = {"f1": D("0.206350"), "f2": D("0.227580"), "f3": D("0.197020")}
    check(
        "Ajo market",
        build_market(AJO, values),
        Market(pun=pun, dispatching=D("0.010501"), capacity=D("0.003197")),
    )
    check(
        "Enel market", build_market(ENEL, values), Market(pun=pun, cdispd=D("0.025043"))
    )
    check("tutela market", build_market(TUTELA, values), Market())
    check("tutela market, no values", build_market(TUTELA, {}), Market())
    raises("missing values are named", build_market, AJO, {"pun_f1": D("0.2")})


def test_units() -> None:
    """Prices are accepted in EUR/kWh or EUR/MWh."""
    for unit, value, expected in (
        ("€/kWh", "0.123456", "0.123456"),
        ("EUR/kWh", "0.123456", "0.123456"),
        ("€/MWh", "123.456", "0.123456"),
        ("EUR/MWh", "123.456", "0.123456"),
        (None, "0.123456", "0.123456"),
    ):
        check(f"price in {unit}", to_eur_per_kwh(D(value), unit), D(expected))
    raises("unknown unit", to_eur_per_kwh, D(1), "W")


def main() -> int:
    """Run every case and report."""
    for test in (test_required, test_build, test_units):
        test()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
