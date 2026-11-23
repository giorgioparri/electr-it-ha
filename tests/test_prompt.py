# SPDX-License-Identifier: GPL-3.0-or-later
"""The AI prompt of the README, and the contracts an AI writes with it.

Run as a script:

    python3 tests/test_prompt.py                     check the prompt in both READMEs
    python3 tests/test_prompt.py ajo answer.json     compare an AI answer with a bill
    python3 tests/test_prompt.py enel answer.json
    python3 tests/test_prompt.py sen answer.json

The prompt sits between the markers below, in a fenced block, in README.md and
README.it.md. Its contract model must be a valid contract, name every field
and every allowed value of the schema, and be the same in both languages but
for its placeholders, so the prompt cannot drift from engine.contract.

The comparison mode reads the JSON an AI answered for one of the bills of
tests/test_bills.py, exactly as it would be pasted in the config flow, and
lists every field that differs from the contract of that bill. Supplier,
offer name and code, discounts and notes are not compared: they do not change
the cost.
"""

import json
import re
import sys
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.contract import (
    Contract,
    ContractError,
    DiscountKind,
    DispatchingMode,
    IndexName,
    IndexPeriod,
    LossMethod,
    PriceType,
    TimeBands,
)
from test_bills import AJO_CONTRACT, ENEL_CONTRACT, SEN_CONTRACT

ROOT = Path(__file__).parents[1]
READMES = (ROOT / "README.md", ROOT / "README.it.md")
START = "<!-- ai-prompt:start -->"
END = "<!-- ai-prompt:end -->"

# Field names the model leaves null or empty: the prompt text must name them.
NESTED_KEYS = (
    "loss_method",
    "fixed_value",
    "capacity_market",
    "eur_kwh",
    "eur_year",
    "optional",
    "kind",
    "condition",
    "valid_months",
)
ENUMS: tuple[type[StrEnum], ...] = (
    PriceType,
    TimeBands,
    IndexName,
    IndexPeriod,
    LossMethod,
    DispatchingMode,
    DiscountKind,
)
BILLS = {"ajo": AJO_CONTRACT, "enel": ENEL_CONTRACT, "sen": SEN_CONTRACT}
NOT_COMPARED = ("supplier", "offer_name", "offer_code", "discounts", "notes")
# Placeholders of the model, written in the language of each README.
TRANSLATED = ("supplier", "offer_name")

_failures: list[str] = []


def check(case: str, what: str, ok: bool, detail: str = "") -> None:
    """Record one check."""
    print(
        f"{'PASS' if ok else 'FAIL'}  {case}: {what}{f' ({detail})' if detail else ''}"
    )
    if not ok:
        _failures.append(f"{case}: {what}")


def prompt_of(readme: Path) -> str | None:
    """Return the fenced block between the markers, or None."""
    text = readme.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        return None
    section = text.split(START, 1)[1].split(END, 1)[0]
    fenced = re.search(r"^```[a-z]*\n(.*?)^```$", section, re.DOTALL | re.MULTILINE)
    return fenced.group(1) if fenced else None


def model_of(prompt: str) -> dict[str, Any] | None:
    """Return the contract model: from the first line "{" to the next line "}"."""
    found = re.search(r"^\{\n.*?^\}$", prompt, re.DOTALL | re.MULTILINE)
    if found is None:
        return None
    try:
        return json.loads(found.group(0), parse_float=Decimal)
    except json.JSONDecodeError:
        return None


def check_readme(readme: Path) -> dict[str, Any] | None:
    """Check the prompt of one README; return its model."""
    case = readme.name
    prompt = prompt_of(readme)
    check(case, "one prompt between the markers, in a fenced block", prompt is not None)
    if prompt is None:
        return None
    model = model_of(prompt)
    check(case, "the prompt holds a JSON contract model", model is not None)
    if model is None:
        return None
    try:
        contract = Contract.from_dict(model)
    except ContractError as err:
        check(case, "the model is a valid contract", False, str(err))
        return model
    check(case, "the model is a valid contract", True)
    missing = sorted(set(contract.to_dict()) - set(model))
    check(case, "the model names every field", not missing, ", ".join(missing))
    unnamed = [key for key in NESTED_KEYS if f'"{key}"' not in prompt]
    check(case, "the prompt names every nested field", not unnamed, ", ".join(unnamed))
    values = [
        f"{enum.__name__}.{value}"
        for enum in ENUMS
        for value in enum
        if f'"{value}"' not in prompt
    ]
    check(case, "the prompt names every allowed value", not values, ", ".join(values))
    return model


def check_prompts() -> None:
    """Check both READMEs, then that they carry the same model."""
    models = [check_readme(readme) for readme in READMES]
    if all(model is not None for model in models):
        english, italian = (
            {key: value for key, value in model.items() if key not in TRANSLATED}
            for model in models
        )
        check("README", "same model in English and Italian", english == italian)


def compared(contract: Contract) -> dict[str, Any]:
    """Return the fields that make the cost."""
    data = contract.to_dict()
    for key in NOT_COMPARED:
        data.pop(key)
    return data


def compare(bill: str, answer: Path) -> None:
    """Compare the JSON an AI wrote for a bill with the contract of that bill."""
    case = f"{bill} <- {answer.name}"
    try:
        written = Contract.from_json(answer.read_text(encoding="utf-8"))
    except ContractError as err:
        check(case, "the answer is a valid contract", False, str(err))
        return
    check(case, "the answer is a valid contract", True)
    expected = compared(Contract.from_dict(BILLS[bill]))
    actual = compared(written)
    for key, value in expected.items():
        check(case, key, actual[key] == value, f"expected {value}, got {actual[key]}")
    if written.notes:
        print("\nNotes written by the AI:")
        for note in written.notes:
            print(f"  - {note}")


def main(argv: list[str]) -> int:
    """Check the prompts, or compare one answer."""
    if not argv:
        check_prompts()
    elif len(argv) == 2 and argv[0] in BILLS:
        compare(argv[0], Path(argv[1]))
    else:
        print(__doc__)
        return 2
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
