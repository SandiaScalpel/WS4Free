# Neighbouring stations

Is your station reading warm, or is it just a warm afternoon? The
**Neighbours** tab (under **Manage**) compares your temperature and humidity
with nearby stations on [Weather Underground](https://www.wunderground.com/),
reading for reading. Its owner, managers and advanced viewers see it (see
[Users and access](users.md)); it's available on stations owned by a site
administrator, because it uses the site's Weather Underground key.

## What you need

Weather Underground gives free API keys only to owners of stations that upload
to it. So:

1. Have your station upload to Weather Underground as well. On Ambient Weather
   consoles that's the awnet app → *Weather Underground*. It doesn't stop the
   console uploading to WS4Free or ambientweather.net.
2. Create a key at
   [wunderground.com/member/api-keys](https://www.wunderground.com/member/api-keys).
3. Whoever runs your WS4Free server adds it to the server's `.env` file as
   `WU_API_KEY=…` and restarts WS4Free (see the README).

Weather Underground allows 1,500 requests a day with that key. Each neighbour
is checked every 10 minutes, which is 144 requests a day, so ten neighbours
fit with room to spare. The tab shows how much of the allowance your list uses.
A server administrator can change how often with `WU_POLL_MINUTES`.

## Choosing neighbours

Find station IDs on [Wundermap](https://www.wunderground.com/wundermap): turn
on the personal weather station layer and click a station. Its ID looks like
`KNMALBUQ123`. Paste the ID, or the station's wunderground.com link, into
**Add a station**. WS4Free checks that the station is reporting before adding it.

Pick five to ten stations:

- **At about your elevation, in similar terrain.** On calm, clear nights cold
  air settles into valleys, so a valley floor and a hillside can differ by
  several degrees with perfect sensors.
- **Within a few miles.** Each station shows its distance and elevation
  difference from yours.

## How the comparison works

Every reading a neighbour reports is matched to your record for the same five
minutes. For each match, WS4Free leaves out the **highest and the lowest**
neighbour reading and **averages the rest**, so one sensor sitting in the sun,
or one in a cold hollow, doesn't move it. (With only one or two neighbours
reporting, it simply averages them.)
Readings that failed Weather Underground's own quality check are ignored.

- **Right now**: your latest reading, the neighbours' average, and the
  difference. The owner (and managers and advanced viewers) also see the
  neighbours' average on the dashboard,
  under *Feels like*.
- **You compared with your neighbours**: over 24 hours, 7 or 30 days, your
  reading minus the neighbours' average, overall and separately for when the sun
  is up and at night. A sensor that reads warm only in sunshine shows up as a
  positive *Sun up* difference with a small *Night* one: the typical sign of a
  poor radiation shield.
- **Charts**: your readings and the neighbours' average over the period, and the
  difference by hour of the day.
- **Compared with the others**, beside each neighbour: how far that station
  usually reads from the rest. A neighbour that's always several degrees off is
  probably badly placed; choose **Leave out** to stop counting it without
  losing its readings, or **Remove** it.

Your readings are compared as they're stored, so a
[temperature calibration](data-quality.md#temperature-calibration) you've
applied is included.

Neighbours' readings are only used for this comparison. They're never shown to
visitors or mixed into your station's data.
