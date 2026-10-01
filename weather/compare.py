"""Station comparison: several stations' history on shared charts.

Each station's series comes from charts.history over the same UTC range, so they
share a resolution (5-minute, hourly or daily) and, for raw and hourly data, the
same bucket times: that lets the page plot one station minus another. Daily
buckets start at each station's own local midnight, so stations in different
time zones only line up at daily resolution if their zones agree.

The summary table reuses the Reports rules (weather.reports): means need ≥ 90 %
of a day, extremes and rain count every measured value.
"""
import datetime as dt

from . import charts, reports

MAX_STATIONS = 6     # one fixed palette slot each, with room to spare in the 8-colour palette
SERIES = ('time', 'temp', 'dewpoint', 'humidity', 'wind', 'gust', 'rain', 'pressure', 'solar')
SUMMARY_COLUMNS = ['temp_high', 'temp_low', 'temp_mean', 'rain', 'gust_max', 'days_with_data']


def compare(stations, start, end, prefs):
    """Series and a summary row per station for (start, end]."""
    out = []
    for station in stations:
        history = charts.history(station, start, end, prefs)
        tz = station.tzinfo
        first_day = start.astimezone(tz).date()
        last_day = (end - dt.timedelta(seconds=1)).astimezone(tz).date()
        report = reports.build(station, first_day, last_day, prefs, group='day', column_keys=SUMMARY_COLUMNS)
        summary = {c.key: (None if v is None else round(v, reports.digits_for(c, prefs)))
                   for c, v in zip(report.columns, report.summary)}
        summary['days'] = (last_day - first_day).days + 1
        out.append({
            'slug': station.slug, 'name': station.name, 'tz': station.timezone,
            'series': {k: history['series'][k] for k in SERIES},
            'summary': summary,
        })
    res = charts.resolution_for(start, end)
    return {
        'resolution': res,
        'start': int(start.timestamp() * 1000),
        'end': int(end.timestamp() * 1000),
        'stations': out,
        # Daily buckets only line up across stations that share a time zone.
        'aligned': res != 'daily' or len({s.timezone for s in stations}) <= 1,
    }
