# WS4Free

Self-hosted history, charts and reports for your personal weather station: a
modern, open-source alternative to WeeWX's reports and paid cloud history
subscriptions. It works with Ambient Weather stations and Ecowitt-protocol
consoles (most Fine Offset–based stations) directly, and with most other
hardware through WeeWX. It keeps every reading on your own server, and looks
good doing it, in light and dark.

![Dashboard](docs/screenshots/dashboard-light.png)

## Features

- **Ingest:** the console's custom-server upload (Ambient, Wunderground and
  Ecowitt formats, and WeeWX), plus the Ambient Weather API to fill gaps and
  import your full history, or an import of an existing WeeWX archive.
- **Live dashboard** that updates itself: temperature, humidity and dew point,
  feels-like (wind chill or heat index), wind compass, rain (today, storm,
  month, year), pressure trend, sun and UV, and 24-hour charts.
- **Forecast** from [Open-Meteo](https://open-meteo.com/) (free, no API key):
  16 days on the dashboard and its own page, and any day hour by hour.
- **Charts:** any date range with synchronised zoom, rain with a running total,
  wind rose, calendar heatmap, year-over-year comparisons, and several
  stations compared side by side.
- **Almanac:** this day in past years, all-time and yearly records (including
  wind chill, heat index and extra sensors), and first and last frost dates.
- **Reports:** daily, monthly or yearly summaries (NOAA-style degree days and
  day counts) with CSV export.
- **Growing:** growing degree days by crop, winter chill, and FAO-56 reference
  evapotranspiration against rainfall.
- **Extra sensors:** extra temperature channels, soil, leaf wetness, air
  quality, CO₂, lightning and leak detectors, each named and public or private;
  piezo and tipping-bucket rain gauges; low-battery warnings.
- **Data quality:** exclude a failing sensor's readings without losing them, or
  calibrate a sensor that reads too warm in the sun, fitted against a nearby
  airport station.
- **Neighbouring stations:** your temperature and humidity against the average
  of nearby Weather Underground stations you choose (highest and lowest left out), by time of day, to spot a
  sensor that reads warm in the sun.
- **Station log:** dated notes (moved, sensor replaced, maintenance…) marked on
  the charts, so later readers know why the data changed.
- **Multiple stations**, each public or private. Per-user display units.
  Two-factor sign-in with passkeys. A user guide built into the app.
- **Honest about gaps:** outages show as breaks, incomplete days never set
  records, and rain totals match the console.

| | |
|---|---|
| ![Charts](docs/screenshots/charts.png) | ![Almanac](docs/screenshots/almanac.png) |
| ![Growing](docs/screenshots/growing.png) | ![Reports](docs/screenshots/reports.png) |
| ![Forecast, hour by hour](docs/screenshots/forecast.png) | ![Dashboard, dark theme](docs/screenshots/dashboard-dark.png) |

<p align="center"><img src="docs/screenshots/mobile.png" alt="Dashboard on a phone, dark theme" width="300"></p>

## Documentation

- **[User guide](docs/guide/index.md)**: using WS4Free, from connecting a
  console to reading the almanac. It's also built into the app under **Help**.
- **[Changelog](CHANGELOG.md)**: what changed in each release, and anything to
  do when upgrading.
- This README covers installing and running the server.

## Docker

The quickest way to run WS4Free is Docker Compose with MySQL:

```bash
git clone <this repository> ws4free && cd ws4free
cp .env.docker.example .env           # set the secret key, passwords and hostnames
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

Then open `http://<your-server>:8000` (or the `WS4FREE_PORT` you chose) and sign
in. Compose runs three containers:

- `db`: MySQL 8.4, with time zone tables loaded automatically.
- `web`: the app, served by Gunicorn. It applies database migrations on start.
- `scheduler`: background jobs. It polls the Ambient API every 5 minutes (if
  you set keys), checks neighbouring Weather Underground stations (if you set
  `WU_API_KEY`), refreshes the summaries every 5 minutes, and runs nightly
  housekeeping.

For PostgreSQL, use `docker compose -f compose.postgres.yaml up -d`. To run
any management command, use `docker compose exec web python manage.py …`.
For example, to import your Ambient history:
`docker compose exec web python manage.py import_ambient_stations --owner <you>`,
then `… backfill_ambient <station-slug>`.

To upgrade, **back up the database first**: a migration can't be undone by
going back to the old code, so the backup is your way back if anything goes
wrong. With the MySQL compose file:

```bash
docker compose exec -T db sh -c 'mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" --single-transaction "$MYSQL_DATABASE"' > ws4free-backup.sql
```

(with PostgreSQL: `docker compose -f compose.postgres.yaml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > ws4free-backup.sql`).
Then:

```bash
git pull && docker compose up -d --build
```

Migrations and static files are handled for you: the `web` container applies
migrations as it starts, the static files are built into the image, and the
`scheduler` waits until the migrations are applied before running any jobs.
The site is unavailable for a few seconds while `web` restarts. Check the
[changelog](CHANGELOG.md)'s *Upgrading* notes for anything else a release needs.

Put an HTTPS reverse proxy in front for anything reachable from the internet,
and leave `/ingest/` reachable over plain HTTP (see
[Production deployment](#production-deployment)). On a home network without
HTTPS, set `DJANGO_HTTPS=False` so you can sign in over `http://`.

## Requirements

- Python 3.12+
- MySQL 8 (reference), PostgreSQL, or SQLite

## Quick start without Docker (development, SQLite)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# in .env set: DJANGO_DEBUG=True, DB_ENGINE=sqlite, DB_NAME=db.sqlite3
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Sign-in uses two-factor authentication (authenticator app or passkey). You are
prompted to set it up after your first login.

## Database setup

### MySQL

```sql
CREATE DATABASE ws4free CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'ws4free'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON ws4free.* TO 'ws4free'@'localhost';
```

#### MySQL time zone tables

WS4Free stores every reading in UTC and builds daily summaries on each station's
**local** calendar day. Django does that with `TruncDay(..., tzinfo=...)`, which
MySQL implements with `CONVERT_TZ()`. A stock MySQL install has **empty** time
zone tables, and then `CONVERT_TZ()` silently returns `NULL`, so every daily
rollup would be `NULL`.

Load the tables once (needs the MySQL root account):

```bash
mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql -u root mysql
```

Then confirm:

```bash
python manage.py check --database default
```

If the tables are missing, this prints warning `weather.W001`. The same check runs
automatically during `migrate`. PostgreSQL and SQLite need no extra setup.

### PostgreSQL

Set `DB_ENGINE=postgresql` and install the driver: `pip install "psycopg[binary]"`.

## Connecting a station

### 1. Create the station

If you use ambientweather.net, put your API key and application key (both from
*Account → API Keys*) in `.env`, then import every device on your account with
its name, location and time zone:

```bash
python manage.py import_ambient_stations --owner <your-username>
```

Otherwise sign in as a staff user and choose **Stations → Add a station** (or
**Add a station** on an empty site): a name, where its readings come from
(Ambient Weather, Ecowitt / Fine Offset, WeeWX or other Wunderground-style
uploads), time zone and location. Only Ambient stations need a MAC address,
and only for the Ambient Weather API.

### 2. Point the console at WS4Free

Open the station's **Manage → Console & uploads** tab. It shows
the exact server, path and port to enter in the console's custom-server upload
settings (the *Customized* screen in Ambient's awnet app, or *Weather Services →
Customized* in Ecowitt's WS View). WS4Free accepts:

| Protocol | Method | Path |
|---|---|---|
| Ambient Weather / Wunderground | GET | `/ingest/ambient/<token>/` |
| Ecowitt | POST | `/ingest/ecowitt/<token>/` |

Use port 80. Consoles cannot upload over HTTPS (see *Production deployment*).
Pushed readings are merged into 5-minute records: peak values (gust, rain rate,
solar, UV) keep the highest reading in each interval, the rest keep the latest.
The token in the path is the credential; the console's own PASSKEY is learned
from its first upload, and uploads with a different one are rejected. Set
`INGEST_CAPTURE=True` while setting up to log every raw upload (Admin → Ingest
captures).

### 3. Import history

#### From WeeWX

```bash
python manage.py import_weewx <station-slug> /var/lib/weewx/weewx.sdb         # SQLite archive
python manage.py import_weewx <station-slug> mysql://user:pass@host/weewx      # MySQL archive
python manage.py import_weewx <station-slug> weewx.sdb --dry-run               # see what would be imported
```

Any WeeWX unit system is converted. Readings WS4Free already has are kept (the
archive only fills gaps), so it's safe to re-run. See the user guide's
[Importing history from WeeWX](docs/guide/connecting.md#importing-history-from-weewx)
for what's imported and how to do it with Docker.

#### From Ambient Weather

```bash
python manage.py backfill_ambient <station-slug>             # everything the API still has
python manage.py backfill_ambient <station-slug> --resume    # continue an interrupted run
```

The API returns at most one day per request and allows about one request per
second, so each year of history takes around 8 minutes. Re-running is safe:
existing records are never changed.

Each request only covers the 24 hours before the date asked for, so a day the
station was offline comes back empty. The backfill steps over such days and
keeps going until `--since`, or (without `--since`) until `--max-empty-days`
consecutive empty days (default 120). Raise that if your station had a longer
outage.

### 4. Schedule the background jobs

| Command | When | What it does |
|---|---|---|
| `poll_ambient` | every 5 min | Fetches the last 24 hours from the Ambient API and fills gaps the console's pushes left (power cuts, Wi-Fi drops) |
| `poll_neighbours` | every 5 min | Fetches neighbouring Weather Underground stations that are due (only with `WU_API_KEY`) |
| `refresh_rollups` | every 5 min | Recomputes rain amounts and the hourly/daily summaries for anything new; takes about a second |
| `ws4free_housekeeping` | daily | Purges old upload logs and login attempts; downsamples old readings if enabled |

With cron, for example:

```cron
1-59/5 * * * *  cd /path/to/ws4free && venv/bin/python manage.py poll_neighbours
*/5 * * * *  cd /path/to/ws4free && venv/bin/python manage.py poll_ambient
2-59/5 * * * *  cd /path/to/ws4free && venv/bin/python manage.py refresh_rollups
30 3 * * *   cd /path/to/ws4free && venv/bin/python manage.py ws4free_housekeeping
```

After a backfill, the first `refresh_rollups` summarises the whole history (a few
minutes for five years). `refresh_rollups --full` rebuilds everything on demand.

## Dashboard, visibility and units

Each station has a live dashboard at `/stations/<slug>/`. It shows the current
conditions and feels-like temperature, today's highs and lows, a 24-hour chart
of temperature, dew point and humidity, wind, rain (today, storm, month,
year), the pressure trend, sun and UV, the forecast, and any extra sensors. It
refreshes every 30 seconds while the tab is visible.

- **Public or private** is set per station in its **Settings**. Public stations
  can be viewed without signing in. Indoor readings, settings, the upload URL
  and the upload log are only ever shown to the owner.
- **Site settings** (staff, from the user menu) set the site title shown in the
  header and browser tab, a tagline, an "about" text, and which station the home
  page shows.
- **Units**: visitors switch between °F and °C in the header. Signed-in users
  can mix units per quantity (e.g. °F with hPa) under *Settings → Units*.
  `DEFAULT_UNIT_SYSTEM` sets the default. Data is always stored in SI units.

## Charts

Each station's **Charts** tab (`/stations/<slug>/charts/`) has:

- **History**: temperature (with dew point, the low–high range, and wind
  chill or heat index while they apply), humidity,
  wind and gust, rain, pressure and solar radiation, for the last 24 hours,
  7 or 30 days, the year to date, the last year, everything, or any custom
  dates. The rain chart also shows, on its right-hand axis, the total
  accumulated since the start of the range. All charts zoom and
  pan together. Long ranges use hourly or daily summaries, and zooming into a
  short enough window loads the full detail. Station outages show as breaks
  in the lines. **Download CSV** exports the range in your units.
- **Wind rose**: share of time the wind blew from each direction, by speed.
- **Calendar**: a year of daily highs, lows or rain. Click a day to open it.
- **Year over year**: cumulative rain, or smoothed daily highs or lows, one line
  per year.

The range is part of the URL, so a view can be bookmarked or shared. Entries
from the station log are marked on the history charts, and the owner also sees
periods when a sensor's battery was low.

On a site with more than one station, **Compare** (in the header) puts up to
six stations on shared charts, with a summary table and a "difference from"
view for comparing microclimates.

## Almanac

Each station's **Almanac** tab has:

- **On this day**: a date (today by default) in every recorded year, with the
  station's own normal high and low (averaged over ±3 days of every year) and
  the record high and low for the date.
- **Records**, all-time or for one year: highest and lowest temperature (with
  the time), warmest night, coldest day, wettest day and month, heaviest rain
  rate, strongest gust, lowest wind chill, highest heat index, pressure
  extremes, highest UV, the longest dry spell, and extremes for extra sensors
  (probe temperatures, particulates, CO₂, lightning).
- **Frost dates**: the last spring and first fall frost (32 °F) or hard freeze
  (28 °F) of each year, the season between them, and averages.

Records are honest about missing data. A measured extreme counts even if the
rest of that day wasn't recorded. Whole-day statistics (coldest day, warmest
night, normals) need at least 90% of the day. A dry spell ends at a day with no
data. A frost date is marked *uncertain* if the station was offline, or missed
the hours before dawn, on a day when a frost could have changed the answer.

## Reports

The **Reports** tab summarises any period (this or last month, this or last
year, everything, or custom dates) by day, month or year. Choose from high,
low, mean and average high/low temperature, heating and cooling degree days,
hot and freeze days, humidity, dew point, rain, rain days, peak rain rate,
wind, gust, pressure, solar, UV and days with data. A "whole period" row
totals or averages them. **Download CSV** gives the same report.

The conventions follow the US National Weather Service monthly climate
summaries:

- degree days from (high + low) / 2, base 65 °F or 18 °C;
- hot days have a high ≥ 90 °F or 32 °C;
- freeze days have a low ≤ 32 °F or 0 °C;
- rain days have ≥ 0.01 in or 0.25 mm.

## Growing

The **Growing** tab is for gardeners and growers:

- **Growing degree days**, by crop preset (corn/general 50–86 °F, small
  grains, alfalfa, cotton, wine grapes / Winkler index): this season so far,
  the average to date from earlier seasons, and a chart comparing every year.
- **Winter chill** for fruit trees: chill hours (32–45 °F) or Utah chill
  units, November–February (May–August in the southern hemisphere).
- **Reference evapotranspiration (ET₀)** by the FAO-56 Penman–Monteith method
  from the station's temperature, humidity, wind, solar radiation, latitude
  and elevation, with a temperature-only Hargreaves estimate alongside, and
  rain versus ET₀ for the last 30 days and by month.

ET₀ assumes the wind sensor sees open-field wind. Set its height in the
station's settings. A sheltered backyard anemometer reads low wind and makes
Penman–Monteith low; the Hargreaves figure isn't affected by siting.

GDD and ET₀ are also available as Reports columns.

## Data quality

Sensors fail: a humidity sensor sticks, a rain gauge clogs, a bird uses the
anemometer. On a station's **Data quality** page the owner can exclude a
period (or an ongoing problem) for chosen measurements. Excluded readings are
set aside, not deleted, so charts, records, frost dates, reports and the
dashboard ignore them. Removing the exclusion restores them exactly. Any
measurement can be excluded, extra sensors included. Almanac records have an
"Exclude these readings…" shortcut for when a record looks wrong.

The **Station log** (Manage → Station log) keeps dated notes about the station:
moved, sensor replaced, maintenance, battery changed, outage. Each entry is
public or private, and is marked on the Charts tab.

## Forecast

The dashboard shows a 16-day forecast from [Open-Meteo](https://open-meteo.com/)
(weather data licensed CC BY 4.0, credited on the page): as many days as fit
on the card, with every day on the station's forecast page
(`/stations/<slug>/forecast/`), and any day hour by hour in a pop-up. No
account or API key is needed, but the server needs outbound HTTPS. It is fetched at most hourly per
station, when the dashboard is viewed, and only the station's location rounded
to two decimals (about 1 km) is sent. Owners can turn it off per station; set
`FORECAST_URL=` (empty) in `.env` to turn it off for the whole site. Open-Meteo's
free API is for non-commercial use.

## Neighbouring stations

A station's **Neighbours** tab (owner only) compares its temperature and
humidity with nearby [Weather Underground](https://www.wunderground.com/)
stations the owner chooses: right now, over 24 hours to 30 days, and by hour of
the day. Each neighbour reading is matched to the station's record for the same
five minutes, and compared with the neighbours' average after leaving out the
highest and the lowest reading, so one badly sited station doesn't skew it.

It needs a Weather Underground API key, which WU gives free to owners of
stations that upload to it (wunderground.com/member/api-keys). Set
`WU_API_KEY` in `.env`; without it the feature is off. The key allows 1,500
requests a day: each neighbour polled every `WU_POLL_MINUTES` (default 10)
uses 144. Only the neighbours' temperature, humidity and dew point are kept,
and only the owner sees them.

## Temperature calibration

A sensor that reads wrong in a consistent way (typically warm in sunshine and
cold on clear nights, from a poor radiation shield) can be corrected on the
**Data quality** page. The correction depends on the time of day and solar
radiation, month by month, and can be fitted automatically against a nearby
airport station using hourly data from the
[Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) (the server
needs outbound HTTPS to fetch it), or entered by hand. Original readings are
kept, so it is fully reversible. See the user guide for details.

## Summaries and storage

Long-range charts, records and reports read hourly and daily summaries rather
than every 5-minute reading. Each summary holds averages (weighted by time),
highs and lows, peak gust, the vector-averaged wind direction, rain total and
peak rain rate. It also records **coverage**: how much of the hour or day had
data, overall and separately for temperature and rain. A day when the outdoor
sensor was offline won't produce a false record low.

Days are the station's local calendar days (23 or 25 hours on DST changes).
Hours are UTC hours.

Five-minute readings take roughly 100,000 rows a year, which MySQL handles
easily, so every reading is kept by default. To save space on long-running
installs, set `RAW_RETENTION_YEARS` (for example `5`). The daily housekeeping
job will then merge readings older than that into 15-minute records, keeping the
true highs and lows, peak gust, rain total and peak rain rate of each 15 minutes.
Preview it with `python manage.py downsample_observations --dry-run`.

### How rain is recorded

Consoles report running totals that reset (daily at local midnight, per storm
event) rather than "rain in the last five minutes". WS4Free derives the rain that
fell during each record from those counters. Within a day it uses the daily
counter, so daily, monthly and yearly totals match the console and
ambientweather.net. Across the midnight reset it uses the event or lifetime
counter, so rain in the last few minutes before midnight is not lost. Records are
stamped with the *end* of their 5-minute interval, as in Ambient's archive and
WeeWX.

## Front-end assets

Styles are written with Tailwind CSS v4. The compiled stylesheet
(`weather/static/weather/css/app.css`) is committed, so **you don't need to build
anything to run WS4Free**. After changing templates or `assets/css/app.css`, rebuild
with the Tailwind standalone CLI (no Node.js needed; the script downloads it):

```bash
./scripts/build-css.sh           # one-off build
./scripts/build-css.sh --watch   # rebuild while editing
```

htmx, Alpine.js, Apache ECharts and the Inter font are bundled under
`weather/static/weather/`, so the app makes no CDN requests.

## Tests

```bash
python manage.py test --settings=test_settings
```

The suite runs on in-memory SQLite and needs no database server.

## Production deployment

Run Gunicorn behind a reverse proxy that terminates HTTPS, for example:

```bash
gunicorn WS4Free.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 120
```

Then set `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`
and the `OTP_WEBAUTHN_*` values in `.env`, and run `python manage.py collectstatic`.
If the site is only reached over plain `http://` on a home network, also set
`DJANGO_HTTPS=False`; otherwise WS4Free redirects to HTTPS and sends cookies only
over HTTPS, and sign-in won't work.

Static files are served by the app itself through [WhiteNoise](https://whitenoise.readthedocs.io/),
compressed and with content-hashed names, so the proxy needs no `/static/` rule.
**Re-run `collectstatic` after every upgrade**: pages fail to render if the static
manifest is missing or out of date.

### Upgrading without Docker

```bash
mysqldump -u ws4free -p --single-transaction ws4free > ws4free-backup.sql   # or pg_dump, or copy the SQLite file
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
# then restart Gunicorn (e.g. systemctl restart ws4free)
```

Keep that order: migrate before restarting so the new code never meets the old
database, and `collectstatic` before restarting so no page asks for a static
file that isn't there yet. The [changelog](CHANGELOG.md)'s *Upgrading* notes
say if a release needs anything more.

**Station uploads arrive over plain HTTP.** Weather station consoles can't use
HTTPS for custom-server uploads and don't follow redirects. WS4Free exempts
`/ingest/` from its own HTTPS redirect. Make sure your proxy, CDN or tunnel does
too ("Always use HTTPS" style rules must skip `/ingest/`).

## License

WS4Free is free software: you can redistribute it and/or modify it under the
terms of the [GNU General Public License](LICENSE) as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version (SPDX: `GPL-3.0-or-later`). If you distribute a modified version,
you must share its source under the same terms.

WS4Free is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

Bundled third-party assets keep their own licenses: htmx (0BSD), Alpine.js
(MIT), Apache ECharts (Apache-2.0) and the Inter font (SIL OFL 1.1). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
