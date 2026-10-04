# Charts

The **Charts** tab shows the station's history for any period.

## Choosing a range

Use the buttons at the top: **24 h**, **7 days**, **30 days**, **Year to
date**, **1 year**, **All**, or **Custom** for any two dates. The range is part
of the page address, so a view can be bookmarked or shared.

Short ranges show every 5-minute reading. Ranges up to 120 days use hourly
summaries, longer ones daily summaries; the badge at the top right says which.
Zoom in far enough and the finer detail loads automatically.

## History

Temperature (with dew point, the low–high range shaded on summaries, and
wind chill and heat index while they apply: on hourly and daily summaries,
the lowest wind chill and highest heat index of each hour or day),
humidity, wind speed and gust, rain, pressure and solar radiation, plus any
[extra sensors](extra-sensors.md) the station has.

- **Zoom**: scroll or pinch on any chart; drag to pan; or use the slider under
  the last chart. All charts move together. **Reset zoom** goes back.
- **Hover** (or tap) for exact values.
- **Rain** shows bars for each 5 minutes, hour or day, and a line, on the
  right-hand scale, of the total since the start of the range.
- **Gaps**: when the station was offline the lines break rather than drawing
  a straight line across the missing time.
- **Low battery** periods are shaded in amber for the station's owner; hover
  inside one to see which sensor it was.
- **Station log** entries (a move, a new sensor…) show as dashed vertical
  markers; hover near one to read it. See [Settings and sharing](settings.md#station-log).
- **Download CSV** saves the range in the units you're viewing.

## Wind rose

How much of the time the wind blew *from* each of 16 directions, split by
speed, for the selected range. **Calm** is the share of time below the lowest
speed band. A sheltered sensor shows less wind from the sheltered side.

## Calendar

A year of daily highs, lows or rain as a heatmap. Click a day to open it in
History. Days with too little data are left blank rather than shown as zero.

## Year over year

One line per year: cumulative rain since January 1, or 7-day averages of daily
highs or lows. Click a year in the legend to hide or show it. Hovering lists
every year, highest value first; put the pointer right on a line to highlight
that year and see only its value.

## Comparing stations

On a site with more than one station, **Compare** (in the header, on the
Stations page, and on each station's Charts tab) shows up to six stations on
the same charts: temperature, dew point, humidity, wind, gust, rain and
pressure, plus solar radiation. Click a station's name to add or remove it.
Each station keeps its own colour.

- **Rain** is shown as the total since the start of the range, so the station
  that got more rain ends higher.
- **Show as difference from…** plots every station minus the first one you
  picked, which is the quickest way to see a microclimate: how much colder the
  orchard is on clear nights than the house, or how much windier the hilltop.
- The **summary** table gives each station's high, low, mean, rain total and
  peak gust for the range, with the same rules for missing data as
  [Reports](reports.md): means only count days with at least 90% of their
  readings, so a station that was offline for part of the range says so in
  *Days with data*.

Stations in different time zones can't be compared as differences at daily
resolution (ranges over 120 days), because their days start at different
times.
