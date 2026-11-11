<p align="center">
  <img src="https://raw.githubusercontent.com/giorgioparri/electr-it-ha/HEAD/images/logo.png" alt="electrITHA" width="320">
</p>

<p align="right"><a href="https://github.com/giorgioparri/electr-it-ha/blob/HEAD/README.it.md">🇮🇹 Leggi in italiano</a></p>

# electrITHA — Home Assistant Custom Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/giorgioparri/electr-it-ha.svg)](https://github.com/giorgioparri/electr-it-ha/releases)
[![Validate](https://github.com/giorgioparri/electr-it-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/giorgioparri/electr-it-ha/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/giorgioparri/electr-it-ha)](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/LICENSE)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=giorgioparri&repository=electr-it-ha&category=integration)

A [Home Assistant](https://www.home-assistant.io/) integration that tells you what the electricity you draw from the grid is costing you, almost as it happens, by applying the rules of an **Italian household supply contract** to your own meter readings: energy price (PUN-indexed, fixed or regulated), network losses, dispatching, transport, system charges, excise and VAT.

> **Why this exists:** the PUN is only a part of what a kWh costs. On the bills this integration is tested against it was between a third and a little over half of the total; the rest is transport, system charges, dispatching, excise and VAT, each with its own rule. This integration applies all of them, the way your supplier does, and is checked against real bills.

---

## ⚠️ Important notes

- **During the month the figures are estimates.** Most indexed contracts bill the *monthly average* of the PUN by time band, which is final only when the month ends. Every estimated value carries an `estimated` attribute.
- **Regulated values are built in, and cover 2026.** Transport, system charges, the regulated service prices and taxes are the ones published by ARERA for each quarter of 2026. For a date outside the covered period the sensors become unknown and say why, rather than guess a tariff. Later periods come with updates of the integration.
- **Household customers in low voltage only.** Residents and non-residents, any committed power.
- **Not applied yet:** discounts, optional green energy, time-limited prices, consumption tiers, Peak/Off-Peak bands.
- The **excise allowance** for residents up to 3 kW follows the law (first 150 kWh per month exempt, reduced above the threshold) but has not been checked against a detailed bill yet.
- Unofficial project, not affiliated with ARERA, GME, Acquirente Unico, Terna or any supplier.

---

## Features

- **Price of the next kWh**, VAT included, for the time band in force — ready for the Energy dashboard
- **Cost of the month so far** and **cost of last month**, computed like a bill: one line per component, each rounded to the cent, VAT line by line
- **Fixed cost per day**: what the supply costs with no consumption
- **Time bands F1 / F2 / F3** with Italian national holidays, and energy drawn in the month split by band
- **One or more meters**: the increases of several cumulative energy sensors are added together (three-phase supplies, more than one measuring point)
- **Any contract** that fits the model, pasted as JSON and validated on the spot; reconfigurable at any time without losing the month's counters
- English and Italian

## Sensors

Each configured supply is a device with six sensors.

| Sensor | What it shows | Unit |
|---|---|---|
| Price of the next kWh | Cost of one more kWh right now, VAT included. Attributes: band, sale, network, system charges, excise, VAT | EUR/kWh |
| Fixed cost per day | Fixed and power charges of today, VAT included | EUR/d |
| Cost this month | The bill from the first of the month to now. Attributes: taxable, VAT, subtotals by section | EUR |
| Cost last month | The bill of the month that ended, computed at the month change | EUR |
| Grid energy this month | kWh drawn in the month. Attributes: kWh in F1, F2, F3 | kWh |
| Time band | The band in force: F1, F2 or F3 | — |

When a value cannot be computed its state is unknown and the `problems` attribute says what is missing.

---

## Installation

### Via HACS (recommended)

1. In HACS open the menu, choose **Custom repositories**, add `https://github.com/giorgioparri/electr-it-ha` with type **Integration**
2. Search for **electrITHA**, install, then restart Home Assistant

### Manual

1. Download `electr_it_ha.zip` from the latest [release](https://github.com/giorgioparri/electr-it-ha/releases/latest)
2. Extract it into `/config/custom_components/electr_it_ha/`
3. Restart Home Assistant

Requires Home Assistant 2026.8.0 or later.

---

## Configuration

**Settings → Devices & services → Add integration → electrITHA.** Three screens:

1. **Supply contract** — paste the contract as JSON (see below). It is validated at once and every problem is listed.
2. **Supply and meters** — whether you are a resident, the committed power in kW, and one or more cumulative energy sensors that measure what you draw from the grid (kWh, Wh or MWh).
3. **Market values** — only the ones your contract is priced with. For each, choose an entity or type a fixed number in €/kWh. An entity may report €/kWh or €/MWh. This screen is skipped when the contract needs none.

To change anything later use **Reconfigure** on the integration entry: the same screens open with the current values.

### The contract

The contract holds only what the supplier decides. Everything regulated comes from the integration.

```json
{
  "schema_version": 1,
  "supplier": "My supplier",
  "offer_name": "My offer",
  "offer_code": null,
  "price_type": "indexed",
  "time_bands": "f1_f2_f3",
  "index": {"name": "pun_index_gme", "period": "monthly", "coefficient": 1.0},
  "energy_price": {"f1": 0.0286, "f2": 0.0286, "f3": 0.011},
  "dispatching": {"mode": "cdispd", "fixed_value": null, "capacity_market": false},
  "disp_bt": true,
  "fixed_fee_eur_year": 144,
  "energy_fee_eur_kwh": null,
  "green_energy": null,
  "discounts": [],
  "notes": []
}
```

| Field | Meaning |
|---|---|
| `price_type` | `indexed` (PUN plus spread), `fixed`, or `regulated` (servizio di tutela) |
| `time_bands` | `mono`, `f1_f23` or `f1_f2_f3` |
| `index` | Only for `indexed`. `coefficient` multiplies the PUN: `1.10` for contracts that bill network losses on top (PUN × (1 + λ) with λ = 10%), `1.0` otherwise |
| `energy_price` | €/kWh by band, losses included, with keys `f0`, or `f1` and `f23`, or `f1`, `f2`, `f3`. For `indexed` it is the spread; for `fixed` the full price; absent for `regulated` |
| `dispatching.mode` | `cdispd` (the monthly CDispD passed through), `regulated` (dispatching billed on kWh plus losses), `fixed` (a value set by the supplier, in `fixed_value`), `included` (inside the energy price), `tutela` (regulated service) |
| `dispatching.capacity_market` | `true` when the capacity market is a separate line |
| `disp_bt` | `true` when the DispBT component is billed |
| `fixed_fee_eur_year` | The supplier's fixed fee, in € per year |
| `energy_fee_eur_kwh` | A supplier fee per kWh on top of the energy price |
| `green_energy` | `{"eur_kwh": …, "eur_year": …, "optional": …}` or `null` |
| `discounts`, `notes` | Stored and shown; discounts are not applied yet |

The model follows the layout that suppliers use to publish their offers on the ARERA Portale Offerte, so any offer on sale can be described with it.

### Market values

| Name | Needed when |
|---|---|
| PUN by band | `price_type` is `indexed`: the monthly average PUN Index GME for each band of the contract |
| Dispatching | `dispatching.mode` is `regulated` |
| Capacity market | `dispatching.capacity_market` is `true` |
| CDispD | `dispatching.mode` is `cdispd` |

They change every month. Until the integration fetches them by itself, point each one to an entity that already has it in your Home Assistant, or type the value from your latest bill and update it when it changes.

### Energy dashboard

In **Settings → Dashboards → Energy**, on your grid consumption source choose **Use an entity with current price** and select *Price of the next kWh*. Fixed charges are not part of the Energy dashboard: they are in *Fixed cost per day* and in *Cost this month*.

---

## How the cost is computed

The calculator rebuilds a bill: every component is a line, each line is rounded to the cent, VAT is computed line by line. Regulated per-kWh components are billed in one line for as long as the tariff does not change; fixed and power charges by the month; excise month by month, on metered kWh only.

It is tested against real bills:

| Bill | Result |
|---|---|
| Indexed to PUN by band, 10% losses, regulated dispatching and capacity market, 6 kW | Taxable amount, VAT and total match to the cent |
| Indexed to PUN plus spread plus CDispD, two months across a tariff change, 5 kW | Every section, VAT and total match to the cent |
| Regulated service, two bands, 3 kW, summary only | Each section within one cent; excise not checked |

The energy between two readings of a meter goes to the time band in force when the second reading arrives: with a meter that reports every few minutes the error is a few minutes of consumption at each band change.

---

## Changelog

See [CHANGELOG.md](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/CHANGELOG.md) for the full version history.

---

## License

GPL-3.0-or-later — see [LICENSE](https://github.com/giorgioparri/electr-it-ha/blob/HEAD/LICENSE)

## Disclaimer

This is an unofficial integration. The figures it shows are estimates meant to help you understand your consumption: the only amount you owe is the one on your supplier's bill. Regulated values are transcribed from ARERA publications; the values approved by the Authority result only from its official acts.

## Acknowledgments

Built with the assistance of [Claude](https://claude.ai) by Anthropic.
