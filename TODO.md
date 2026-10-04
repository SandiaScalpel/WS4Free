# To do

The WS4Free roadmap: what's planned, roughly in order. Finished items move to
the [changelog](CHANGELOG.md). Suggestions are welcome as GitHub issues.

## Features

- **F2. WeatherFlow Tempest.** Receive the hub's local network broadcasts (and/or
  use the Tempest cloud API). Tempest reports rain per interval rather than as
  running totals, so it needs its own rain handling (as WeeWX imports already
  have). Needs a "Tempest" choice for where a station's readings come from.
- **F3. Davis WeatherLink Live / console.** Poll the device's local
  `current_conditions` API. (Davis owners can already connect through WeeWX,
  and import their WeeWX history.)

## Housekeeping

- **H1. New README screenshots** showing the forecast, the Temperature &
  Humidity card and the station comparison.
- **H2. GitHub release pages** for each tag, with that version's changelog.
- **H3. Docker:** fix anything the first real-world Docker installs turn up.
