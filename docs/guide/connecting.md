# Connecting a station

WS4Free receives readings in up to three ways:

1. **Console uploads** ("custom server" or "customized" upload): the console
   sends a reading every 16–60 seconds straight to your WS4Free server. This
   is the main source and works without the internet.
2. **The Ambient Weather API**, for Ambient stations: fills gaps the uploads
   missed (a Wi-Fi drop, a server restart) and imports your history.
3. **WeeWX**, for hardware WS4Free doesn't talk to directly (Davis, Oregon
   Scientific, La Crosse and others): WeeWX reads the station and forwards
   its records.

## Adding the station

A station has to exist in WS4Free before it can receive readings.

- **In the app:** a site administrator chooses **Stations → Add a station** (or
  **Add a station** on an empty site). Give it a name, choose where its readings
  come from (an Ambient Weather console, an Ecowitt or Fine Offset console,
  WeeWX, or other Wunderground-style uploads), and set the time zone, latitude
  and longitude. Only Ambient stations need a MAC address, and only to use the
  Ambient Weather API.
- **Ambient Weather accounts:** the administrator can instead run
  `import_ambient_stations`, which adds every device on your ambientweather.net
  account with its name, location and time zone (see the README).

Then, as the owner, open the station and check **Manage → Settings**. **Readings
come from** decides which setup steps the **Console & uploads** tab shows and
which settings apply, and can be changed later. Time zone, latitude, longitude
and elevation matter for daily totals, the forecast and evapotranspiration.

## Ambient Weather consoles

1. Open the station's **Console & uploads** tab. It shows the server, path and
   port for this station.
2. In the **awnet** app, select your console, then **Customized**. Turn it on
   and choose **Ambient Weather** as the protocol.
3. Enter the server (your WS4Free host name), the path exactly as shown
   (including the long code), and port **80**. Set the upload interval to 16–60
   seconds.

Within a minute, **Latest reading** on the same page should update.

## Ecowitt consoles and gateways

This includes Ecowitt and Fine Offset clones (Froggit, Misol, Sainlogic and
others) and gateways such as the GW1100 and GW2000.

1. In **WS View Plus** (or the gateway's web page), go to **Weather Services →
   Customized**.
2. Choose **Ecowitt** as the protocol, enter the server, the **Ecowitt path**
   from the **Console & uploads** tab, and port **80**.
3. Set the upload interval (16–60 seconds) and save.

Both US and metric uploads work. A console with a **piezo rain sensor** (the
WS90 "WittBoy" and similar) is recorded too. If yours has both a piezo sensor
and a tipping-bucket gauge, choose which one to record under **Settings → Data
sources → Rain sensor**. *Automatic* uses the tipping bucket.

## WeeWX

[WeeWX](https://weewx.com) supports a long list of station hardware. Its
Wunderground uploader can send each archive record to WS4Free instead of (not
as well as) Weather Underground. In `weewx.conf`:

```ini
[StdRESTful]
    [[Wunderground]]
        enable = true
        station = MYSTATION
        password = anything
        server_url = http://your-ws4free-host/ingest/ambient/YOUR-STATION-CODE/
```

With **Readings come from** set to WeeWX, the station's **Console & uploads**
tab shows this snippet with your server and path filled in. `station` and `password` can be anything: WS4Free
remembers the `station` value from the first upload and rejects uploads with a
different one. Restart WeeWX; readings arrive with each archive record (every
5 minutes by default).

WeeWX's uploader sends temperature, humidity, dew point, pressure, wind, daily
rain, solar radiation, UV, and its first soil moisture, soil temperature, leaf
wetness and PM2.5 sensors. It doesn't send a storm or lifetime rain counter, so
rain in the last few minutes before midnight can occasionally land in the next
day.

## Importing history from WeeWX

If WeeWX has been recording your station, its archive database can be imported
so WS4Free starts with your full history. The server administrator runs:

```bash
python manage.py import_weewx <station> /var/lib/weewx/weewx.sdb        # SQLite archive
python manage.py import_weewx <station> mysql://user:password@host/weewx  # MySQL archive
```

- `--dry-run` shows how many records there are, the date range, and which
  measurements will be imported, without changing anything.
- Readings WS4Free already has are kept; the archive only fills the gaps, so
  it's safe to re-run, and safe to import after uploads have started.
- Records in any WeeWX unit system (US, METRIC or METRICWX) are converted.
- Imported: temperature, humidity, dew point, pressure, wind, rain and rain
  rate, solar radiation, UV, indoor temperature and humidity, extra
  temperature and humidity channels, soil temperature, soil moisture (as soil
  tension in centibars), PM2.5, PM10, CO₂, lightning and battery status. The
  dry run lists any columns that aren't imported (for example Davis leaf
  wetness).
- The summaries are rebuilt in the background afterwards, a few minutes for
  several years of data.

With Docker, copy the file into the container first, for example
`docker compose cp weewx.sdb web:/app/data/` and then
`docker compose exec web python manage.py import_weewx <station> /app/data/weewx.sdb`.

## Plain HTTP for uploads

Consoles can't upload over HTTPS, so uploads use plain HTTP on port 80 even
when the website itself uses HTTPS. The long code in the path is the station's
password: anyone with it can upload readings to your station. Don't share it.
If it leaks, choose **Generate new URL** on the **Console & uploads**
tab and update the console.

The console's own PASSKEY (or WeeWX's `station`) is learned from its first
upload. Uploads with a different one are rejected and listed in the upload log.
After replacing a console, use **Replaced the console? Forget it** so the new
one is accepted.

## Troubleshooting

- **Nothing arrives.** Check the server name and port 80 in the console, and
  that the path was copied completely. The **Recent upload log** shows rejected
  uploads and why. Your administrator can turn on `INGEST_CAPTURE` to log every
  upload while setting up.
- **"PASSKEY does not match"** in the log: another device is using this
  station's path, or you replaced the console (see above).
- **"Console clock implausible"**: the console's time was more than 15 minutes
  off, so the server's time was used. Check the console's time settings.
- **The reverse proxy redirects to HTTPS.** Uploads under `/ingest/` must stay
  reachable over plain HTTP; see the README's production deployment notes.

## Importing history (Ambient Weather)

If your station uploads to ambientweather.net, the administrator can import
everything Ambient still has with `backfill_ambient`. It takes roughly 8
minutes per year of history and is safe to re-run. Afterwards the server
summarises the history in the background, which can take a few minutes; charts
and records fill in when it finishes. See the README for the commands.
