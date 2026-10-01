"""Agricultural calculations: growing degree days, chill, reference evapotranspiration.

Everything is computed from the rollups (SI), then converted for display.

GDD — the "modified" method used by US extension services: the daily high is
capped (e.g. 86 °F for corn) and the low floored at the base before averaging,
GDD = max(0, (high' + low') / 2 − base).

Chill — the 0–45 °F model (hours with 32 °F < T ≤ 45 °F) and the Utah model
(Richardson et al. 1974), from hourly mean temperatures.

ET₀ — FAO Irrigation and Drainage Paper 56 (Allen et al. 1998), daily
Penman–Monteith (eq. 6) for a grass reference surface; Hargreaves (eq. 52) when
solar, humidity or wind data are incomplete for the day.
"""
import datetime as dt
import math
from dataclasses import dataclass

from .models import DailyRollup, HourlyRollup

DAY_COVERAGE = 0.9


# ── Growing degree days ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GddPreset:
    key: str
    label: str
    base_f: float
    cap_f: float = None
    start: tuple = (1, 1)          # (month, day) the season starts
    end: tuple = (12, 31)


GDD_PRESETS = [
    GddPreset('general', 'Corn / general (50–86 °F)', 50, 86),
    GddPreset('small_grains', 'Wheat, barley, oats (32 °F)', 32),
    GddPreset('alfalfa', 'Alfalfa (41 °F)', 41),
    GddPreset('cotton', 'Cotton (60–86 °F)', 60, 86),
    GddPreset('grapes', 'Wine grapes, Winkler index (50 °F, Apr–Oct)', 50, None, (4, 1), (10, 31)),
]
GDD_BY_KEY = {p.key: p for p in GDD_PRESETS}


def f_to_c(f):
    return (f - 32) * 5 / 9


def gdd_day(tmax_c, tmin_c, base_c, cap_c=None):
    """Modified growing degree days (°C·days) for one day."""
    if tmax_c is None or tmin_c is None:
        return None
    hi = min(tmax_c, cap_c) if cap_c is not None else tmax_c
    hi = max(hi, base_c)
    lo = max(tmin_c, base_c)
    if cap_c is not None:
        lo = min(lo, cap_c)
    return max(0.0, (hi + lo) / 2 - base_c)


def _in_season(month_day, start, end):
    """Whether (month, day) falls in a season that may wrap past New Year."""
    if start <= end:
        return start <= month_day <= end
    return month_day >= start or month_day <= end


def _season_dates(year, start, end):
    first = dt.date(year, *start)
    try:
        last = dt.date(year if end >= start else year + 1, *end)
    except ValueError:                       # Feb 29 in a common year
        last = dt.date(year if end >= start else year + 1, end[0], 28)
    return first, last


def gdd_seasons(station, base_c, cap_c, start=(1, 1), end=(12, 31), today=None):
    """Per year: cumulative GDD (°C·days) for each day of the season, None where the
    day's temperature data is incomplete (the total carries on without it)."""
    rows = {r.date: r for r in DailyRollup.objects.filter(station=station)
            .only('date', 'temp_max_c', 'temp_min_c', 'temp_coverage')}
    if not rows:
        return []
    today = today or max(rows)
    seasons = []
    for year in range(min(rows).year, today.year + 1):
        first, last = _season_dates(year, start, end)
        if first > today:
            continue
        total, values, missing = 0.0, [], 0
        d = first
        while d <= min(last, today):
            r = rows.get(d)
            g = gdd_day(r.temp_max_c, r.temp_min_c, base_c, cap_c) if r and r.temp_coverage >= DAY_COVERAGE else None
            if g is None:
                missing += 1
                values.append(None)
            else:
                total += g
                values.append(total)
            d += dt.timedelta(days=1)
        if any(v is not None for v in values):
            seasons.append({'year': year, 'start': first, 'values': values, 'total': total, 'missing': missing,
                            'complete': last <= today})
    return seasons


def average_to_date(seasons, index, current_year, max_missing=5):
    """Mean cumulative value at season day `index` over earlier seasons that have it
    and are missing at most `max_missing` days up to it."""
    vals = []
    for s in seasons:
        if s['year'] == current_year or len(s['values']) <= index:
            continue
        upto = s['values'][:index + 1]
        if upto[-1] is not None and sum(v is None for v in upto) <= max_missing:
            vals.append(upto[-1])
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


# ── Chill ────────────────────────────────────────────────────────────────────

CHILL_UPPER_C = (45 - 32) * 5 / 9      # exactly 45 °F; a rounded 7.22 would drop 45 °F itself


def chill_hour(temp_c):
    """0–45 °F model: one hour when 32 °F < T ≤ 45 °F."""
    return 1.0 if 0.0 < temp_c <= CHILL_UPPER_C else 0.0


def utah_units(temp_c):
    """Utah model (Richardson et al. 1974), chill units for one hour."""
    t = temp_c * 9 / 5 + 32
    if t <= 34:
        return 0.0
    if t <= 36:
        return 0.5
    if t <= 48:
        return 1.0
    if t <= 54:
        return 0.5
    if t <= 60:
        return 0.0
    if t <= 65:
        return -0.5
    return -1.0


CHILL_MODELS = {'hours': ('Chill hours (32–45 °F)', chill_hour), 'utah': ('Utah chill units', utah_units)}


def _local_day_hours(day, tz):
    start = dt.datetime.combine(day, dt.time(), tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz).astimezone(dt.UTC)
    return round((end - start).total_seconds() / 3600)


def chill_seasons(station, model='hours', today=None):
    """Per winter: cumulative chill by day, Nov 1 → Feb 28/29 (May 1 → Aug 31 south of
    the equator), from hourly mean temperatures. Hours without data add nothing."""
    func = CHILL_MODELS[model][1]
    south = station.latitude is not None and station.latitude < 0
    start, end = ((5, 1), (8, 31)) if south else ((11, 1), (2, 29))
    tz = station.tzinfo
    per_day, hours_per_day = {}, {}
    for period_start, temp in (HourlyRollup.objects.filter(station=station, temp_avg_c__isnull=False)
                               .values_list('period_start', 'temp_avg_c').iterator(chunk_size=5000)):
        local = period_start.astimezone(tz).date()
        if not _in_season((local.month, local.day), start, end):
            continue
        per_day[local] = per_day.get(local, 0.0) + func(temp)
        hours_per_day[local] = hours_per_day.get(local, 0) + 1
    if not per_day:
        return []
    today = today or max(per_day)
    seasons = []
    first_year = min(per_day).year - (0 if south else 1)
    for year in range(first_year, today.year + 1):
        first, last = _season_dates(year, start, end)
        if first > today:
            continue
        total, values, missing_hours = 0.0, [], 0
        d = first
        while d <= min(last, today):
            day_hours = _local_day_hours(d, tz)          # 23 or 25 on DST changes
            if d in per_day:
                total += per_day[d]
                values.append(round(total, 1))
                missing_hours += max(0, day_hours - hours_per_day[d])
            else:
                values.append(None)
                missing_hours += day_hours
            d += dt.timedelta(days=1)
        if any(v is not None for v in values):
            label = f'{year}–{str(year + 1)[-2:]}' if not south else str(year)
            seasons.append({'year': year, 'label': label, 'start': first, 'values': values, 'total': total,
                            'missing_hours': missing_hours, 'complete': last <= today})
    return seasons


# ── Reference evapotranspiration (FAO-56) ────────────────────────────────────

SIGMA = 4.903e-9          # Stefan-Boltzmann, MJ K⁻⁴ m⁻² day⁻¹
GSC = 0.0820              # solar constant, MJ m⁻² min⁻¹


def _es(t):
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def extraterrestrial_radiation(lat_deg, day_of_year):
    """Ra, MJ m⁻² day⁻¹ (FAO-56 eq. 21)."""
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    delta = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    return 24 * 60 / math.pi * GSC * dr * (ws * math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.sin(ws))


def wind_at_2m(speed, height_m):
    """FAO-56 eq. 47: wind measured at height z → 2 m."""
    if height_m is None or abs(height_m - 2.0) < 1e-6:
        return speed
    return speed * 4.87 / math.log(67.8 * height_m - 5.42)


def et0_penman_monteith(tmax, tmin, rh_max, rh_min, u_z, rs, lat, elev, doy, z_wind=2.0, tdew=None):
    """Daily grass-reference ET₀, mm/day (FAO-56 eq. 6). rs in MJ m⁻² day⁻¹.
    Actual vapour pressure from RHmax/RHmin (eq. 17), else from dew point (eq. 14)."""
    t = (tmax + tmin) / 2
    p = 101.3 * ((293 - 0.0065 * (elev or 0)) / 293) ** 5.26
    gamma = 0.000665 * p
    delta = 4098 * _es(t) / (t + 237.3) ** 2
    es = (_es(tmax) + _es(tmin)) / 2
    if rh_max is not None and rh_min is not None:
        ea = (_es(tmin) * rh_max / 100 + _es(tmax) * rh_min / 100) / 2
    elif tdew is not None:
        ea = _es(tdew)
    else:
        return None
    u2 = wind_at_2m(u_z, z_wind)
    ra = extraterrestrial_radiation(lat, doy)
    rso = (0.75 + 2e-5 * (elev or 0)) * ra
    rns = (1 - 0.23) * rs
    ratio = min(rs / rso, 1.0) if rso > 0 else 0.0
    rnl = SIGMA * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2 * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * ratio - 0.35)
    rn = rns - rnl
    et0 = (0.408 * delta * rn + gamma * 900 / (t + 273) * u2 * (es - ea)) / (delta + gamma * (1 + 0.34 * u2))
    return max(0.0, et0)


def et0_hargreaves(tmax, tmin, lat, doy):
    """FAO-56 eq. 52, mm/day — temperature and latitude only."""
    ra = extraterrestrial_radiation(lat, doy)
    t = (tmax + tmin) / 2
    return max(0.0, 0.0023 * (t + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * ra * 0.408)


def et0_for_rollup(r, station):
    """(mm/day, method) for one DailyRollup, or (None, None) when it can't be estimated."""
    if station.latitude is None or r.temp_coverage < DAY_COVERAGE or r.temp_max_c is None or r.temp_min_c is None:
        return None, None
    lat, doy = float(station.latitude), r.date.timetuple().tm_yday
    full = r.coverage >= DAY_COVERAGE
    if full and r.solar_avg_wm2 is not None and r.wind_speed_avg_ms is not None and (
            (r.humidity_min is not None and r.humidity_max is not None) or r.dewpoint_avg_c is not None):
        rs = r.solar_avg_wm2 * 0.0864          # mean W/m² over 24 h → MJ m⁻² day⁻¹
        value = et0_penman_monteith(r.temp_max_c, r.temp_min_c, r.humidity_max, r.humidity_min, r.wind_speed_avg_ms,
                                    rs, lat, station.elevation_m, doy, station.anemometer_height_m, r.dewpoint_avg_c)
        if value is not None:
            return value, 'pm'
    return et0_hargreaves(r.temp_max_c, r.temp_min_c, lat, doy), 'hargreaves'


def et0_days(station, start, end):
    """[(date, et0 mm or None, method, rain mm or None)] for each day in [start, end]."""
    from .charts import rain_known
    rows = {r.date: r for r in DailyRollup.objects.filter(station=station, date__gte=start, date__lte=end)}
    out, d = [], start
    while d <= end:
        r = rows.get(d)
        if r is None:
            out.append((d, None, None, None))
        else:
            et0, method = et0_for_rollup(r, station)
            rain = r.rain_mm if rain_known(r.rain_mm, r.rain_coverage, DAY_COVERAGE) else None
            out.append((d, et0, method, rain))
        d += dt.timedelta(days=1)
    return out
