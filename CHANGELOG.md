# Changelog

What changed in each release of WS4Free, newest first. The version you're
running is shown at the bottom of every page. Upgrading? See *Upgrading* under
each release for anything you need to do beyond the usual steps in the README.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## 0.7.0 — 2026-10-02

### Added

- **Forecast** on the dashboard, beside the indoor tile: as many days as fit,
  each with an icon (clear, partly cloudy, rain, showers, thunderstorms, snow,
  fog…), the high and low, the chance of rain and the expected amount. Data
  from Open-Meteo (free, no API key), refreshed hourly. Owners can turn it off
  per station under **Settings → Data sources**; `FORECAST_URL=` turns it off
  site-wide.

### Upgrading

- Run `migrate`. The server needs outbound HTTPS to `api.open-meteo.com`.

## 0.6.0 — 2026-10-02

### Added

- **Wind chill and heat index**: the dashboard's *feels like* says which one
  applies; the Charts temperature chart shows them while they apply (lowest
  wind chill and highest heat index per hour or day on summaries), and the CSV
  download has both columns; the almanac records the lowest wind chill and
  highest heat index, with the time.

### Fixed

- Signing in with a passkey left you on the sign-in page (signed in). It now
  goes to the page you were trying to open, or the home page.

### Upgrading

- Run `migrate`. The summaries of your whole history are then rebuilt once in
  the background to add wind chill and heat index (a few minutes for several
  years of data).

## 0.5.0 — 2026-10-01

### Added

- **Compare stations**: up to six stations on shared charts (temperature, dew
  point, humidity, wind, gust, accumulated rain, pressure, solar), with a
  summary table and a "difference from" view for microclimates. Linked from the
  header, the Stations page and each station's Charts tab when there's more
  than one station.

### Changed

- The dashboard's temperature card is now **Temperature & Humidity**: its
  24-hour chart shows temperature (yellow-orange), dew point (blue) and
  humidity (aqua, on its own 0–100% scale), with a legend and all three values
  in the tooltip.

## 0.4.0 — 2026-10-01

### Added

- A **Stations** page listing every station you can see, linked from the header
  when there's more than one.

### Changed

- Header: **Help** moved to the right, beside the units switch. The **Charts**
  and **Dashboard** links were removed (the site name goes home, and every
  station page has its own Charts tab); old links to `/charts/` still work.
- The site's tagline is shown beside the site title in the header, instead of
  above the station name.

### Fixed

- On the home page's list of stations, each station now opens when clicked.
  Visitors previously had no way to open a public station from the list.

## 0.3.0 — 2026-10-01

### Added

- **User guide in the app**: a **Help** link in the header, and on every
  station page a link to the matching section. The same pages are in
  `docs/guide/` for reading on GitHub.
- **This changelog**, linked from the version number at the bottom of every page.
- **Extra sensors**: extra temperature and humidity channels, soil temperature
  and moisture, leaf wetness, PM2.5 and PM10, CO₂, lightning and leak
  detectors, from Ambient Weather, Ecowitt and WeeWX uploads. They appear on
  the dashboard under **More sensors**, get their own charts and CSV columns,
  and can be named and made public or private one by one under **Settings →
  Extra sensors**. The owner also sees low-battery warnings.
- **Piezo rain sensors** (Ecowitt WS90 and similar). With both a piezo sensor
  and a tipping-bucket gauge, choose which to record under **Settings → Data
  sources → Rain sensor**.
- **Metric Ecowitt uploads** (`tempc`, `baromrelhpa`, `windspeedkmh`,
  `dailyrainmm`…) are now understood.
- **WeeWX** can send readings to WS4Free through its Wunderground uploader;
  see the guide's *Connecting a station*.

### Upgrading

- Install the new dependency (`pip install -r requirements.txt`; Docker
  images do this for you) and run `migrate`.
- If your station already has extra sensors, their history is summarised once
  in the background after the first upload that reports them. This takes a
  few minutes for several years of data.

## 0.2.0 — 2026-10-01

### Added

- **Temperature calibration** (Station → Data quality) for a sensor that reads
  wrong in a consistent way, such as warm in sunshine and cold on clear nights.
  The correction uses the time of day and solar radiation, month by month. Fit
  it against a nearby airport station (hourly data from the Iowa Environmental
  Mesonet), review the before-and-after error, then apply; or enter the numbers
  yourself. Fully reversible. Corrected periods are labelled on the charts,
  almanac, reports and growing pages.

### Changed

- WS4Free is licensed under the **GNU General Public License, version 3 or
  later** (GPL-3.0-or-later).

### Upgrading

- Run `migrate`.

## 0.1.0 — 2026-10-01

First public release.

- Console uploads in the Ambient Weather, Wunderground and Ecowitt formats;
  the Ambient Weather API for filling gaps and importing history.
- Live dashboard, charts (any range, wind rose, calendar, year over year),
  almanac (this day in history, records, frost dates), reports with CSV export,
  and growing statistics (degree days, winter chill, evapotranspiration).
- Data-quality exclusions for failing sensors.
- Several stations, each public or private; per-user units; light and dark
  themes; two-step sign-in with passkeys.
- MySQL, PostgreSQL or SQLite; Docker Compose files for MySQL and PostgreSQL.
