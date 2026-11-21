# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-10

### Added

- First release: the cost of the electricity drawn from the grid under an Italian household supply contract, computed from your own meters.
- Six sensors per supply: price of the next kWh (VAT included, ready for the Energy dashboard), fixed cost per day, cost of the month so far, cost of last month, grid energy of the month by time band, and the time band in force.
- Contracts indexed to the PUN (with or without the loss multiplier), at a fixed price, or in the regulated service; single band, two bands or three bands; dispatching as CDispD, regulated, fixed or included, with or without a separate capacity market line.
- Network losses are billed the way your supplier does: as separate lines of loss kWh (`index.loss_method` `energy`, the default) or inside the PUN price (`price`). Check which one your bill uses: with the wrong one the energy part can be off by a few cents.
- The contract is pasted as JSON and validated on the spot; supply, meters and the source of each market value (an entity or a fixed number, in €/kWh or €/MWh) are chosen in the UI and can be changed at any time with Reconfigure.
- One or more grid import meters in kWh, Wh or MWh: their increases are added together and split into F1, F2 and F3, Italian national holidays included.
- Regulated values for every quarter of 2026 are built in: transport and metering, UC3 and UC6, ASOS and ARIM, DispBT, regulated service prices, excise and VAT. For a date outside 2026 the sensors become unknown and say why.
- The bill calculator is tested against real bills: two match to the cent, a third within one cent per section.
- The PUN by band can be downloaded from the official GME API with your own GME account, added once and shared by every supply: ten sensors with the average PUN of this month and last month, suggested in the config flow.
- The GME account screens explain that the temporary password GME emails cannot be used as is, link to GME's password portal to change it, and say that it must be renewed every 6 months; a reauthentication prompt is shown whenever GME refuses the saved password.
- Dispatching, capacity market and CDispD do not need to be typed: when their field is left empty, the values published by Terna (every quarter) and ARERA (every month) are used. A workflow of the repository collects them once a day into `market_values.json` on the `market-data` branch; the integration downloads that file once a day, keeps the last good copy in Home Assistant, and ships a copy of its own. While a month runs, a value not yet published for it is replaced by the last one published, and the figures are marked `estimated`.
- A repair is shown when the published values cannot be downloaded for three days; it goes away by itself at the first download that works.
- A month that ends is billed with the market values of that month: sensors that report their month, like those of the GME account, are used once they refer to it and are complete. It waits up to three days for them, or up to 15 days for a published value, since ARERA publishes the CDispD of a month only after it ends; then it is billed with the last values available.
- English and Italian.

[Unreleased]: https://github.com/giorgioparri/electr-it-ha/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/giorgioparri/electr-it-ha/releases/tag/v0.1.0
