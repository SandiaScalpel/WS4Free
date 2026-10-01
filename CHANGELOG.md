# Changelog

What changed in each release of WS4Free, newest first. The version you're
running is shown at the bottom of every page. Upgrading? See *Upgrading* under
each release for anything you need to do beyond the usual steps in the README.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

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
