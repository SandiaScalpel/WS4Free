# How the numbers work

WS4Free tries hard to be honest about what the station actually measured.
This page explains the rules behind the totals, records and charts.

## Readings and records

Consoles upload every 16–60 seconds. WS4Free merges those into one record per
5 minutes, the same interval as ambientweather.net and WeeWX. Within each
5 minutes, **peak** measurements (gust, rain rate, solar radiation, UV) keep
the highest value seen; the others keep the latest.

Each record is stamped with the **end** of its 5 minutes, as in
ambientweather.net's archive and WeeWX: the record at 12:05 covers 12:00 to
12:05, and the one at midnight belongs to the day that's ending.

## Days and time zones

Every day is the station's **local** calendar day, in the time zone set in its
settings, whoever is viewing. A day when daylight saving time starts or ends is
23 or 25 hours long, and is treated that way.

## Rain

Consoles don't report "rain in the last 5 minutes". They report running
totals: today's rain (reset at midnight), the current storm, and a lifetime
total. WS4Free works out the rain that fell in each record from the change in
those totals:

- within a day, from the daily total, so daily, monthly and yearly figures match
  the console and ambientweather.net;
- across midnight, from the storm or lifetime total, so rain in the last few
  minutes before the reset isn't lost.

## Missing data

Stations go offline: a power cut, a flat battery, a Wi-Fi outage. WS4Free never
fills the gap with guesses:

- charts show a break;
- a measured extreme (the highest temperature, the strongest gust) counts
  even if the rest of the day is missing, because it really happened;
- whole-day figures (the day's mean, warmest night, degree days, normals)
  need at least 90% of the day;
- a day's rain is shown if any rain was measured; a **zero** only counts if
  enough of the day was recorded, because no data isn't the same as dry;
- the longest dry spell ends at a day without data.

## Summaries

Long ranges, records and reports use hourly and daily summaries rather than
every 5-minute record. Each summary keeps averages (weighted by time), true
highs and lows, the peak gust, the average wind direction (weighted by speed),
the rain total and peak rain rate, and how much of the period had data.
Summaries are updated every five minutes, so a brand-new reading can take a
few minutes to appear in records and reports, though the dashboard shows it
immediately.

## Corrections

Readings you exclude, and temperatures you calibrate, are never deleted: the
originals are kept so either can be undone. See
[Data quality and calibration](data-quality.md).
