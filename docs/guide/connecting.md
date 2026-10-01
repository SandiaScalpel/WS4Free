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

- **Ambient Weather:** the server administrator runs
  `import_ambient_stations`, which adds every device on your ambientweather.net
  account with its name, location and time zone (see the README).
- **Anything else:** the administrator adds it under **Admin → Stations** with a
  name, time zone and MAC address. For an Ecowitt console, use the MAC shown in
  the WS View app or on the console. A WeeWX station has no MAC that WS4Free
  uses: enter any unique made-up one, such as `02:00:00:00:00:01`, and turn off
  **Fill gaps from ambientweather.net** in the station's settings.

Then, as the owner, open the station, choose **Manage**, and check its
**Settings**: time zone, latitude, longitude and elevation matter for daily
totals, sunrise and evapotranspiration.

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

Use the **Ambient** path from the station's **Console & uploads** tab after
`http://your-ws4free-host`. `station` and `password` can be anything: WS4Free
remembers the `station` value from the first upload and rejects uploads with a
different one. Restart WeeWX; readings arrive with each archive record (every
5 minutes by default).

WeeWX's uploader sends temperature, humidity, dew point, pressure, wind, daily
rain, solar radiation, UV, and its first soil moisture, soil temperature, leaf
wetness and PM2.5 sensors. It doesn't send a storm or lifetime rain counter, so
rain in the last few minutes before midnight can occasionally land in the next
day.

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
