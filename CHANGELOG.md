# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-10

### Added

- First release: the cost of the electricity drawn from the grid under an Italian household supply contract, computed from your own meters.
- Six sensors per supply: price of the next kWh (VAT included, ready for the Energy dashboard), fixed cost per day, cost of the month so far, cost of last month, grid energy of the month by time band, and the time band in force.
- Contracts indexed to the PUN (with or without the loss multiplier), at a fixed price, or in the regulated service; single band, two bands or three bands; dispatching as CDispD, regulated, fixed or included, with or without a separate capacity market line.
- The contract is pasted as JSON and validated on the spot; supply, meters and the source of each market value (an entity or a fixed number, in €/kWh or €/MWh) are chosen in the UI and can be changed at any time with Reconfigure.
- One or more grid import meters in kWh, Wh or MWh: their increases are added together and split into F1, F2 and F3, Italian national holidays included.
- Regulated values for every quarter of 2026 are built in: transport and metering, UC3 and UC6, ASOS and ARIM, DispBT, regulated service prices, excise and VAT. For a date outside 2026 the sensors become unknown and say why.
- The bill calculator is tested against real bills: two match to the cent, a third within one cent per section.
- English and Italian.

[Unreleased]: https://github.com/giorgioparri/electr-it-ha/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/giorgioparri/electr-it-ha/releases/tag/v0.1.0
