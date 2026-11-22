# SPDX-License-Identifier: GPL-3.0-or-later
"""The reader and converter of the Portale Offerte open data. Run as a script:

    python3 tests/test_offers.py

The offers in tests/fixtures/portale_offerte_20261121.xml are real ones, copied
unchanged from the file the Portale published on 21 November 2026; each is
preceded by a comment naming its case. The cases the file has no example of
are built here by editing one of those offers, the way the specification of
the format ("Funzionamento e Specifiche del Processo di Trasmissione Offerte
Mercato Retail", v5.0) allows them to be written.
"""

import io
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parents[1] / "custom_components" / "electr_it_ha")
)

from engine.contract import Contract, Discount, DiscountKind, GreenEnergy
from engine.offers import Conversion, Offer, Status, convert, parse_offers

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "portale_offerte_20261121.xml"
NAMESPACE = "http://www.acquirenteunico.it/schemas/SII_AU/OffertaRetail/01"
_failures: list[str] = []


def check(what: str, actual: object, expected: object) -> None:
    """Compare a value with the expected one."""
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {what}: {actual!r}, expected {expected!r}")
    if not ok:
        _failures.append(what)


def contains(what: str, texts: tuple[str, ...], fragment: str) -> None:
    """Check that one of `texts` mentions `fragment`, ignoring case."""
    ok = any(fragment.lower() in text.lower() for text in texts)
    print(f"{'PASS' if ok else 'FAIL'}  {what}: mentions {fragment!r}")
    if not ok:
        print(f"      in {texts!r}")
        _failures.append(what)


# --- Fixtures -----------------------------------------------------------------


def fixture_blocks() -> dict[str, str]:
    """Return the XML of every offer in the fixture, by case name."""
    text = FIXTURE.read_text(encoding="utf-8")
    pattern = r"<!-- (\w+) -->\s*(<offerta>.*?</offerta>)"
    return dict(re.findall(pattern, text, re.S))


BLOCKS = fixture_blocks()


def document(*offers: str) -> io.BytesIO:
    """A file of the Portale holding the given offers."""
    body = "\n".join(offers)
    text = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<ListaOfferteMercatoLibero xmlns="{NAMESPACE}">\n{body}\n'
        "</ListaOfferteMercatoLibero>\n"
    )
    return io.BytesIO(text.encode("utf-8"))


def edited(case: str, old: str, new: str) -> Offer:
    """Read the offer of `case` after replacing `old` with `new` in its XML."""
    block = BLOCKS[case]
    if old not in block:
        raise AssertionError(f"{case}: {old!r} is not in the fixture")
    (offer,) = parse_offers(document(block.replace(old, new, 1)))
    return offer


OFFERS = {offer.info.code: offer for offer in parse_offers(FIXTURE)}
CODES = {
    "enel_flex": "000142ESVFL01XXResECAn0000119094",
    "ajo_leggerissima": "030205ESVFL10XXXLEGGERXULTRA1026",
    "coefficient": "038226ESVFL01XXOKCASAENERGIA0000",
    "fixed_mono": "020536ESFML01XXOTT2026FSEPAD1026",
    "fixed_discount": "035372ESFFL04XXLMEGAFIXASMARTWEB",
    "f1_f23": "025434ESFFL15XX00000001054940126",
    "quarterly": "023034ESVFL03XXCONZEROSPREAD0126",
    "bimonthly": "004319ESVFL03XXPEPUNSTDO01012026",
    "peak": "029878ESVFL03XX000000DOM8VAR8BIO",
    "tiers": "000208ESFML04XX000IEDOSTPN261130",
    "timed": "029748ESFML01XX261119LD10X000000",
    "tutela_pcv": "028284ESVML01XX000MERPOWERDOMDIG",
    "regulated_nocode": "027184ESFML01XXB2611DOE0FFIXCANA",
    "disp_regulated": "041498ESVFL04XX0000LUCEPUNVLDOM1",
    "disp_99": "024492ESVFL03XX000000ALTIENERGIA",
    "disp_02": "000184ESVFL11XXGPINDEX02LUCEXPUN",
    "optional_fee": "001145ESVML01XXX26LUCEWEBPLUS000",
    "green": "025434ESFML15XX00000001054930126",
}


def converted(case: str) -> Conversion:
    """Convert the fixture offer of `case`."""
    return convert(OFFERS[CODES[case]])


# --- Reading ------------------------------------------------------------------


def test_reading() -> None:
    """Every offer is read, in order, from a path or from a binary file."""
    check("offers in the fixture", list(OFFERS), list(CODES.values()))
    check("cases in the fixture", list(BLOCKS), list(CODES))
    from_file = [offer.info.code for offer in parse_offers(document(*BLOCKS.values()))]
    check("the same offers from a file object", from_file, list(CODES.values()))
    check("an empty list", list(parse_offers(document())), [])


def test_info() -> None:
    """What the suggester will filter on is kept, as the Portale states it."""
    info = OFFERS[CODES["enel_flex"]].info
    check("Enel: code", info.code, "000142ESVFL01XXResECAn0000119094")
    check("Enel: name", info.name, "ENEL FLEX LUCE")
    check("Enel: VAT number", info.vat_number, "06655971007")
    check("Enel: supplier is the site's domain", info.supplier, "enel.it")
    check(
        "Enel: offer page",
        info.offer_url,
        "https://www.enel.it/it-it/offerte-luce/enel-flex-luce",
    )
    check("Enel: domestic", info.domestic, True)
    check("Enel: sold on its own", info.single, True)
    check("Enel: no residence stated means all", info.residence, "all")
    check("Enel: valid from", info.valid_from, datetime(2026, 11, 12, 10, 0, 0))
    check("Enel: valid to", info.valid_to, datetime(2026, 12, 3, 23, 59, 59))
    check("Enel: duration", info.duration_months, 12)
    check("Enel: no power limits", (info.power_min_kw, info.power_max_kw), (None, None))

    info = OFFERS[CODES["ajo_leggerissima"]].info
    check("Ajo: supplier without scheme and slash", info.supplier, "ajoenergia.it")
    check("Ajo: residence 03 means all", info.residence, "all")
    check("Ajo: power limits", (info.power_min_kw, info.power_max_kw), (D("1.5"), D(6)))

    info = OFFERS[CODES["coefficient"]].info
    check("OK Energia: duration -1 is open-ended", info.duration_months, None)

    info = OFFERS[CODES["disp_02"]].info
    check(
        "Gas Pollino: consumption limits as stated",
        (info.consumption_min_kwh, info.consumption_max_kwh),
        (0, 0),
    )

    info = OFFERS[CODES["tutela_pcv"]].info
    check(
        "Meridionale Gas: zones are kept",
        bool(info.regions or info.provinces or info.municipalities),
        True,
    )

    offer = edited("fixed_mono", "<TIPO_CLIENTE>01<", "<TIPO_CLIENTE>02<")
    check("other uses are not domestic", offer.info.domestic, False)
    offer = edited("fixed_mono", "<DOMESTICO_RESIDENTE>03<", "<DOMESTICO_RESIDENTE>01<")
    check("residence 01", offer.info.residence, "resident")
    offer = edited("fixed_mono", "<DOMESTICO_RESIDENTE>03<", "<DOMESTICO_RESIDENTE>02<")
    check("residence 02", offer.info.residence, "non_resident")


# --- Conversion of the real offers ---------------------------------------------

# What every contract converted from the fixture carries, unless a case says
# otherwise: the domestic CdispD with the DispBT, no per-kWh fee, no green
# energy option, no discount.
BASE = {
    "schema_version": 1,
    "dispatching": {"mode": "cdispd", "fixed_value": None, "capacity_market": False},
    "disp_bt": True,
    "energy_fee_eur_kwh": None,
    "green_energy": None,
    "discounts": [],
}


def pun(period: str, coefficient: float = 1.1) -> dict[str, object]:
    """The PUN Index GME with losses in the price."""
    return {
        "name": "pun_index_gme",
        "period": period,
        "coefficient": coefficient,
        "loss_method": "price",
    }


EXPECTED: dict[str, tuple[Status, dict[str, object]]] = {
    "enel_flex": (
        Status.COMPLETE,
        {
            "supplier": "enel.it",
            "offer_name": "ENEL FLEX LUCE",
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            # MACROAREA 06, standard: Enel's spread, "Contributo al consumo".
            "energy_price": {"f1": 0.02, "f2": 0.02, "f3": 0.02},
            "fixed_fee_eur_year": 144.0,
        },
    ),
    "ajo_leggerissima": (
        Status.COMPLETE,
        {
            "supplier": "ajoenergia.it",
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            "energy_price": {"f1": 0.05, "f2": 0.05, "f3": 0.05},
            "fixed_fee_eur_year": 72.0,
        },
    ),
    "coefficient": (
        Status.COMPLETE,
        {
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            "energy_price": {"f1": 0.01, "f2": 0.01, "f3": 0.01},
            "fixed_fee_eur_year": 120.0,
        },
    ),
    "fixed_mono": (
        Status.COMPLETE,
        {
            "supplier": "enermed.it",
            "price_type": "fixed",
            "time_bands": "mono",
            "index": None,
            "energy_price": {"f0": 0.181},
            "fixed_fee_eur_year": 144.0,
        },
    ),
    "fixed_discount": (
        Status.COMPLETE,
        {
            "price_type": "fixed",
            "time_bands": "mono",
            # MACROAREA 02, the only per-kWh component of a fixed offer.
            "energy_price": {"f0": 0.1351},
            "fixed_fee_eur_year": 144.0,
            "discounts": [
                {
                    "name": "Sconto Invio Email",
                    "kind": "per_year",
                    "value": 6.0,
                    "condition": "electronic billing",
                    "valid_months": None,
                }
            ],
        },
    ),
    "f1_f23": (
        Status.COMPLETE,
        {
            "price_type": "fixed",
            "time_bands": "f1_f23",
            "energy_price": {"f1": 0.22497, "f23": 0.21276},
            "fixed_fee_eur_year": 48.0,
        },
    ),
    "quarterly": (
        Status.COMPLETE,
        {
            "price_type": "indexed",
            "time_bands": "f1_f23",
            "index": pun("quarterly"),
            "energy_price": {"f1": 0.0, "f23": 0.0},
            "fixed_fee_eur_year": 216.0,
        },
    ),
    "bimonthly": (
        Status.COMPLETE,
        {
            "price_type": "indexed",
            "time_bands": "f1_f23",
            "index": pun("bimonthly"),
            "energy_price": {"f1": 0.0253, "f23": 0.0253},
            "fixed_fee_eur_year": 120.0,
        },
    ),
    "peak": (
        Status.PARTIAL,
        {
            "price_type": "indexed",
            "time_bands": "mono",
            "index": pun("monthly"),
            "energy_price": {"f0": 0.0242},
            "fixed_fee_eur_year": 192.0,
        },
    ),
    "tiers": (
        Status.PARTIAL,
        {
            "price_type": "fixed",
            "time_bands": "mono",
            # The interval with no tier: the default the Portale falls back on.
            "energy_price": {"f0": 0.1793},
            "fixed_fee_eur_year": 159.0,
        },
    ),
    "timed": (
        Status.COMPLETE,
        {
            "price_type": "fixed",
            "time_bands": "mono",
            "energy_price": {"f0": 0.129},
            "fixed_fee_eur_year": 108.0,
        },
    ),
    "tutela_pcv": (
        Status.PARTIAL,
        {
            "price_type": "indexed",
            "time_bands": "mono",
            "index": pun("monthly"),
            "energy_price": {"f0": 0.022},
            "fixed_fee_eur_year": 72.0,
        },
    ),
    "regulated_nocode": (
        Status.COMPLETE,
        {
            "price_type": "fixed",
            "time_bands": "mono",
            "energy_price": {"f0": 0.193},
            "fixed_fee_eur_year": 144.0,
        },
    ),
    "disp_regulated": (
        Status.COMPLETE,
        {
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            "energy_price": {"f1": 0.044, "f2": 0.044, "f3": 0.044},
            "dispatching": {
                "mode": "regulated",
                "fixed_value": None,
                "capacity_market": True,
            },
            "disp_bt": True,
            "fixed_fee_eur_year": 168.0,
        },
    ),
    "disp_99": (
        Status.PARTIAL,
        {
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            # 0.187 for F3 is what the supplier sent: data is not corrected.
            "energy_price": {"f1": 0.0187, "f2": 0.0187, "f3": 0.187},
            "dispatching": {
                "mode": "regulated",
                "fixed_value": None,
                "capacity_market": True,
            },
            "disp_bt": True,
            "fixed_fee_eur_year": 102.0,
        },
    ),
    "disp_02": (
        Status.PARTIAL,
        {
            "price_type": "indexed",
            "time_bands": "f1_f2_f3",
            "index": pun("monthly"),
            "energy_price": {"f1": 0.02, "f2": 0.02, "f3": 0.02},
            "fixed_fee_eur_year": 108.0,
        },
    ),
    "optional_fee": (
        Status.COMPLETE,
        {
            "price_type": "indexed",
            "time_bands": "mono",
            "index": pun("monthly"),
            # TIPOLOGIA 02 outside MACROAREA 06 means nothing: both count.
            "energy_price": {"f0": 0.0154},
            "fixed_fee_eur_year": 50.0,
        },
    ),
    "green": (
        Status.COMPLETE,
        {
            "price_type": "fixed",
            "time_bands": "mono",
            # MACROAREA 06, standard: the price of the energy itself.
            "energy_price": {"f0": 0.21703},
            "fixed_fee_eur_year": 48.0,
        },
    ),
}

# What a partial conversion must explain.
PROBLEMS = {
    "peak": "Peak",
    "tiers": "tier",
    "tutela_pcv": "PCV",
    "disp_99": "99",
    "disp_02": "PD",
}

# What a note must say.
NOTES = {
    "enel_flex": ("Contributo al consumo", "1.10"),
    "ajo_leggerissima": ("SPREAD", "ONERI COMMERCIALI", "1.10"),
    "coefficient": ("1.1",),
    "timed": ("Prezzo luce",),
    "fixed_discount": ("Prezzo energia",),
}


def test_conversion() -> None:
    """Each real offer converts to the contract the specification describes."""
    for case, (status, fields) in EXPECTED.items():
        result = converted(case)
        check(f"{case}: status", result.status, status)
        if result.contract is None:
            check(f"{case}: a contract", None, "a contract")
            continue
        data = result.contract.to_dict()
        check(f"{case}: offer code", data["offer_code"], CODES[case])
        for key, value in (BASE | fields).items():
            check(f"{case}: {key}", data[key], value)
        if status is Status.COMPLETE:
            check(f"{case}: no problems", result.problems, ())
        else:
            contains(f"{case}: problem explained", result.problems, PROBLEMS[case])
        check(f"{case}: no warnings yet", result.warnings, ())
        for fragment in NOTES.get(case, ()):
            contains(f"{case}: note", result.contract.notes, fragment)
        contains(f"{case}: source noted", result.contract.notes, "Portale Offerte")


def test_json() -> None:
    """The JSON shown in the contract screen validates as it is."""
    for case in EXPECTED:
        result = converted(case)
        text = result.contract_json()
        check(f"{case}: JSON validates", Contract.from_json(text), result.contract)
        check(f"{case}: JSON is indented", text.startswith("{\n  "), True)
    contract = converted("enel_flex").contract_json()
    check("plain UTF-8, not escaped", "\\u" in contract, False)


# --- Cases the fixture has no example of ----------------------------------------

COMPONENT = """<ComponenteImpresa>
            <NOME>{name}</NOME>
            <DESCRIZIONE>{name}</DESCRIZIONE>
            <TIPOLOGIA>{kind}</TIPOLOGIA>
            <MACROAREA>{area}</MACROAREA>
            <IntervalloPrezzi>{band}
                <PREZZO>{price}</PREZZO>
                <UNITA_MISURA>{unit}</UNITA_MISURA>
            </IntervalloPrezzi>
        </ComponenteImpresa>
        <ComponenteImpresa>"""


def with_component(
    case: str, name: str, kind: str, area: str, unit: str, price: str, band: str = ""
) -> Offer:
    """The offer of `case` with one more company component, placed first."""
    field = (
        f"\n                <FASCIA_COMPONENTE>{band}</FASCIA_COMPONENTE>"
        if band
        else ""
    )
    component = COMPONENT.format(
        name=name, kind=kind, area=area, unit=unit, price=price, band=field
    )
    return edited(case, "<ComponenteImpresa>", component)


def test_components() -> None:
    """Optional green energy, power fees and one-off amounts."""
    result = convert(
        with_component("fixed_mono", "Energia verde", "02", "06", "03", "0.010", "01")
    )
    check("optional green: status", result.status, Status.COMPLETE)
    assert result.contract is not None
    check(
        "optional green: an option, not in the price",
        result.contract.green_energy,
        GreenEnergy(eur_kwh=D("0.010"), eur_year=None, optional=True),
    )
    check(
        "optional green: price unchanged",
        result.contract.energy_price,
        {"f0": D("0.181")},
    )

    result = convert(
        with_component("fixed_mono", "Quota potenza", "01", "01", "02", "10")
    )
    check("fee per kW: partial", result.status, Status.PARTIAL)
    contains("fee per kW: explained", result.problems, "kW")
    assert result.contract is not None
    check(
        "fee per kW: yearly fee unchanged", result.contract.fixed_fee_eur_year, D(144)
    )

    result = convert(
        with_component("fixed_mono", "Contributo attivazione", "01", "05", "05", "30")
    )
    check("one-off amount: complete", result.status, Status.COMPLETE)
    assert result.contract is not None
    contains("one-off amount: noted", result.contract.notes, "Contributo attivazione")
    check(
        "one-off amount: not a yearly fee", result.contract.fixed_fee_eur_year, D(144)
    )


def test_coefficient() -> None:
    """A coefficient below 1 is not losses: it scales the index, losses on top."""
    offer = edited(
        "coefficient",
        "<COEFFICIENTE>1.1</COEFFICIENTE>",
        "<COEFFICIENTE>0.9</COEFFICIENTE>",
    )
    result = convert(offer)
    check("coefficient 0.9: partial", result.status, Status.PARTIAL)
    contains("coefficient 0.9: explained", result.problems, "0.9")
    assert result.contract is not None and result.contract.index is not None
    check("coefficient 0.9 with losses", result.contract.index.coefficient, D("0.99"))

    offer = edited(
        "coefficient",
        "<COEFFICIENTE>1.1</COEFFICIENTE>",
        "<COEFFICIENTE>1</COEFFICIENTE>",
    )
    result = convert(offer)
    check("coefficient 1: complete", result.status, Status.COMPLETE)
    assert result.contract is not None and result.contract.index is not None
    check(
        "coefficient 1: standard losses", result.contract.index.coefficient, D("1.10")
    )
    contains("coefficient 1: assumption noted", result.contract.notes, "1.10")


DISPATCHING_14 = """<Dispacciamento>
            <TIPO_DISPACCIAMENTO>14</TIPO_DISPACCIAMENTO>
            <NOME>CdispD</NOME>
        </Dispacciamento>"""


def test_dispatching() -> None:
    """A supplier's own dispatching value is stated with losses; ours without."""
    own = """<Dispacciamento>
            <TIPO_DISPACCIAMENTO>99</TIPO_DISPACCIAMENTO>
            <VALORE_DISP>0.013200</VALORE_DISP>
            <NOME>Dispacciamento</NOME>
        </Dispacciamento>"""
    result = convert(edited("fixed_mono", DISPATCHING_14, own))
    check("99 alone: complete", result.status, Status.COMPLETE)
    assert result.contract is not None
    data = result.contract.to_dict()
    check(
        "99 alone: fixed, net of losses",
        data["dispatching"],
        {"mode": "fixed", "fixed_value": 0.012, "capacity_market": False},
    )
    check("99 alone: no DispBT unless stated", data["disp_bt"], False)

    regulated = """<Dispacciamento>
            <TIPO_DISPACCIAMENTO>01</TIPO_DISPACCIAMENTO>
            <NOME>Dispacciamento</NOME>
        </Dispacciamento>"""
    result = convert(edited("fixed_mono", DISPATCHING_14, regulated))
    check("01 alone: complete", result.status, Status.COMPLETE)
    assert result.contract is not None
    data = result.contract.to_dict()
    check(
        "01 alone: regulated, no capacity market",
        data["dispatching"],
        {"mode": "regulated", "fixed_value": None, "capacity_market": False},
    )
    check("01 alone: no DispBT unless stated", data["disp_bt"], False)


SCONTO = """<PrezziSconto>
                <TIPOLOGIA>01</TIPOLOGIA>
                <UNITA_MISURA>01</UNITA_MISURA>
                <PREZZO>6</PREZZO>
            </PrezziSconto>"""


def discount_of(old: str, new: str) -> Conversion:
    """Convert the discount offer after one edit to its discount."""
    return convert(edited("fixed_discount", old, new))


def test_discounts() -> None:
    """Each unit of measure gives its kind; conditions are written out."""
    for unit, kind in (("03", "per_kwh"), ("05", "one_off"), ("06", "percent")):
        result = discount_of(
            "<UNITA_MISURA>01</UNITA_MISURA>\n                <PREZZO>6<",
            f"<UNITA_MISURA>{unit}</UNITA_MISURA>\n                <PREZZO>6<",
        )
        assert result.contract is not None
        (discount,) = result.contract.discounts
        check(f"discount unit {unit}", discount.kind, DiscountKind(kind))

    result = discount_of(
        "<CONDIZIONE_APPLICAZIONE>01</CONDIZIONE_APPLICAZIONE>",
        "<CONDIZIONE_APPLICAZIONE>99</CONDIZIONE_APPLICAZIONE>\n"
        "                <DESCRIZIONE_CONDIZIONE>Solo con bolletta web"
        "</DESCRIZIONE_CONDIZIONE>",
    )
    assert result.contract is not None
    check(
        "discount condition 99: its description",
        result.contract.discounts[0].condition,
        "Solo con bolletta web",
    )

    result = discount_of(
        "<CONDIZIONE_APPLICAZIONE>01</CONDIZIONE_APPLICAZIONE>",
        "<CONDIZIONE_APPLICAZIONE>00</CONDIZIONE_APPLICAZIONE>",
    )
    assert result.contract is not None
    check("discount without condition", result.contract.discounts[0].condition, None)

    result = discount_of(
        "<IVA_SCONTO>01</IVA_SCONTO>",
        "<IVA_SCONTO>01</IVA_SCONTO>\n            <PeriodoValidita>\n"
        "                <DURATA>12</DURATA>\n            </PeriodoValidita>",
    )
    assert result.contract is not None
    check(
        "discount for 12 months",
        result.contract.discounts[0],
        Discount(
            name="Sconto Invio Email",
            kind=DiscountKind.PER_YEAR,
            value=D(6),
            condition="electronic billing",
            valid_months=12,
        ),
    )

    for code, fragment in (("02", "power"), ("04", "tutela")):
        result = discount_of(
            SCONTO, SCONTO.replace("<TIPOLOGIA>01<", f"<TIPOLOGIA>{code}<")
        )
        check(f"discount type {code}: complete", result.status, Status.COMPLETE)
        assert result.contract is not None
        check(f"discount type {code}: not a discount", result.contract.discounts, ())
        contains(f"discount type {code}: noted", result.contract.notes, fragment)


def test_unsupported() -> None:
    """Offers the contract model cannot hold give no contract, and say why."""
    cases: dict[str, tuple[str, str, str]] = {
        "FLAT": ("fixed_mono", "<TIPO_OFFERTA>01<", "<TIPO_OFFERTA>03<"),
        "mixed": ("fixed_mono", "<TIPO_OFFERTA>01<", "<TIPO_OFFERTA>04<"),
        "all-inclusive": (
            "fixed_mono",
            "<TIPO_OFFERTA>01</TIPO_OFFERTA>",
            "<TIPO_OFFERTA>01</TIPO_OFFERTA>\n"
            "            <OFFERTA_ONNICOMPRENSIVA>01</OFFERTA_ONNICOMPRENSIVA>",
        ),
        "other index": (
            "ajo_leggerissima",
            "<IDX_PREZZO_ENERGIA>12<",
            "<IDX_PREZZO_ENERGIA>99<",
        ),
        "sum of indices": (
            "ajo_leggerissima",
            "</RiferimentiPrezzoEnergia>",
            "</RiferimentiPrezzoEnergia>\n        <RiferimentiPrezzoEnergia>\n"
            "            <IDX_PREZZO_ENERGIA>01</IDX_PREZZO_ENERGIA>\n"
            "        </RiferimentiPrezzoEnergia>",
        ),
        "gas": ("fixed_mono", "<TIPO_MERCATO>01<", "<TIPO_MERCATO>02<"),
    }
    for name, (case, old, new) in cases.items():
        result = convert(edited(case, old, new))
        check(f"{name}: unsupported", result.status, Status.UNSUPPORTED)
        check(f"{name}: no contract", result.contract, None)
        check(f"{name}: no JSON", result.contract_json(), None)
        check(f"{name}: a reason", len(result.problems) > 0, True)


def test_whole_file_shape() -> None:
    """The conversion is total: no offer of the fixture raises."""
    statuses = [convert(offer).status for offer in parse_offers(FIXTURE)]
    counts = {status: statuses.count(status) for status in Status}
    check(
        "statuses",
        counts,
        {Status.COMPLETE: 13, Status.PARTIAL: 5, Status.UNSUPPORTED: 0},
    )
    json.dumps([convert(offer).contract_json() for offer in OFFERS.values()])


def run(test: Callable[[], None]) -> None:
    """Run one test, turning an exception into a failure."""
    try:
        test()
    except Exception as err:  # a crash is a failed test, not the end of the run
        print(f"FAIL  {test.__name__}: {type(err).__name__}: {err}")
        _failures.append(test.__name__)


def main() -> int:
    """Run every test and report."""
    for test in (
        test_reading,
        test_info,
        test_conversion,
        test_json,
        test_components,
        test_coefficient,
        test_dispatching,
        test_discounts,
        test_unsupported,
        test_whole_file_shape,
    ):
        run(test)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
