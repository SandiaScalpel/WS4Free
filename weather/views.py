import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import redirect_to_login
from django.db.models import Max, Min, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import agro, almanac, charts, dashboard, reports
from . import quality
from .forms import ExclusionForm, SiteSettingsForm, StationSettingsForm
from .ingest.store import mark_dirty
from .models import DailyRollup, DataExclusion, Observation, SiteSettings, Station
from .units import SYSTEMS, UNITS_COOKIE, prefs_for_request


def visible_stations(user):
    """Public stations plus, for a signed-in user, their own (staff see all)."""
    if user.is_authenticated and user.is_staff:
        return Station.objects.all()
    if user.is_authenticated:
        return Station.objects.filter(Q(is_public=True) | Q(owner=user))
    return Station.objects.filter(is_public=True)


def _owned_station(request, slug):
    station = get_object_or_404(Station, slug=slug)
    if not (request.user.is_staff or station.owner_id == request.user.pk):
        raise Http404
    return station


def _home_station(request, stations):
    """The station the site's front page shows, or None for the station list."""
    site = SiteSettings.get()
    if site.default_station_id and any(s.pk == site.default_station_id for s in stations):
        return next(s for s in stations if s.pk == site.default_station_id)
    public = [s for s in stations if s.is_public]
    if len(stations) == 1:
        return stations[0]
    if len(public) == 1 and not request.user.is_authenticated:
        return public[0]
    return None


def _render_dashboard(request, station, stations, is_home=False):
    context = dashboard.build(station, prefs_for_request(request), viewer=request.user)
    context['other_stations'] = [s for s in stations if s.pk != station.pk]
    context['is_home'] = is_home
    return render(request, 'weather/dashboard.html', context)


def home(request):
    stations = list(visible_stations(request.user).select_related('latest'))
    station = _home_station(request, stations)
    if station is not None:
        return _render_dashboard(request, station, stations, is_home=True)
    return render(request, 'weather/home.html', {'stations': stations})


def charts_home(request):
    """Header 'Charts' link: the home page's station, else the station list."""
    stations = list(visible_stations(request.user))
    station = _home_station(request, stations)
    if station is None:
        return redirect('weather:home')
    return redirect('weather:station-charts', slug=station.slug)


def _viewable_station(request, slug):
    station = get_object_or_404(Station, slug=slug)
    if station.is_public or (request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk)):
        return station
    if not request.user.is_authenticated:
        raise _LoginRequired
    raise Http404


class _LoginRequired(Exception):
    pass


def station_dashboard(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    return _render_dashboard(request, station, list(visible_stations(request.user)))


def station_live(request, slug):
    """The dashboard's live block, polled by htmx every 30 s."""
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        raise Http404
    context = dashboard.build(station, prefs_for_request(request), viewer=request.user)
    return render(request, 'weather/partials/live.html', context)


@require_POST
def set_units(request):
    """Header °F/°C switch: imperial or metric for everything. Signed-in users keep it
    in their profile (Settings allows mixing units); visitors get a cookie."""
    system = request.POST.get('system')
    target = request.POST.get('next') or '/'
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = '/'
    response = redirect(target)
    if system not in SYSTEMS:
        return response
    if request.user.is_authenticated:
        from accounts.models import UserProfile
        UserProfile.objects.filter(user=request.user).update(**{f'unit_{q}': v for q, v in SYSTEMS[system].items()})
    else:
        response.set_cookie(UNITS_COOKIE, system, max_age=365 * 86400, samesite='Lax',
                            secure=not settings.DEBUG, httponly=True)
    return response


@user_passes_test(lambda u: u.is_active and u.is_staff)
def site_settings(request):
    site = SiteSettings.get()
    form = SiteSettingsForm(request.POST or None, instance=site)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Site settings saved.')
        return redirect('weather:site-settings')
    return render(request, 'weather/site_settings.html', {'form': form})


@login_required
def station_setup(request, slug):
    station = _owned_station(request, slug)
    span = Observation.objects.filter(station=station).aggregate(oldest=Min('timestamp'), newest=Max('timestamp'))
    host = request.get_host().split(':')[0]
    return render(request, 'weather/station_setup.html', {
        'station': station,
        'host': host,
        'ambient_path': f'/ingest/ambient/{station.push_token}/',
        'ecowitt_path': f'/ingest/ecowitt/{station.push_token}/',
        'span': span,
        'latest': getattr(station, 'latest', None),
        'captures': station.captures.all()[:15],
        'tab': 'console',
    })


@login_required
def station_settings(request, slug):
    station = _owned_station(request, slug)
    old_timezone, was_public = station.timezone, station.is_public
    form = StationSettingsForm(request.POST or None, instance=station, prefs=prefs_for_request(request))
    if request.method == 'POST' and form.is_valid():
        station = form.save()
        if station.timezone != old_timezone:
            # Local days moved: every daily rollup and every midnight rain boundary
            # may change. The rollup job recomputes rain and summaries from here.
            oldest = Observation.objects.filter(station=station).order_by('timestamp').values_list('timestamp', flat=True).first()
            if oldest:
                mark_dirty(station.pk, oldest)
            messages.info(request, f'Time zone changed to {station.timezone}. Daily totals will be recalculated in the background.')
        if station.is_public and not was_public:
            messages.success(request, f'{station.name} is now public. Anyone can view its dashboard and charts.')
        elif was_public and not station.is_public:
            messages.success(request, f'{station.name} is now private. Only you can see it.')
        else:
            messages.success(request, 'Settings saved.')
        return redirect('weather:station-settings', slug=station.slug)
    # On a failed POST the form has already copied the submitted values onto
    # `station`; the header must show what is actually saved.
    return render(request, 'weather/station_settings.html', {
        'station': Station.objects.get(pk=station.pk), 'form': form, 'tab': 'settings',
        'timezones': StationSettingsForm.timezone_choices(),
    })


@login_required
@require_POST
def station_forget_passkey(request, slug):
    station = _owned_station(request, slug)
    Station.objects.filter(pk=station.pk).update(push_passkey='')
    messages.info(request, 'Console PASSKEY forgotten. The next upload will be accepted and its PASSKEY learned.')
    return redirect('weather:station-setup', slug=station.slug)


@login_required
@require_POST
def station_rotate_token(request, slug):
    station = _owned_station(request, slug)
    station.rotate_push_token()
    messages.warning(request, 'New upload URL generated. Update the path on your console — the old one no longer works.')
    return redirect('weather:station-setup', slug=station.slug)


# ── Charts ────────────────────────────────────────────────────────────────────

RANGE_PRESETS = {'24h': dt.timedelta(hours=24), '7d': dt.timedelta(days=7), '30d': dt.timedelta(days=30),
                 '1y': dt.timedelta(days=365)}


def _parse_range(request, station):
    """(start, end) UTC from ?range=24h|7d|30d|ytd|1y|all or ?start=YYYY-MM-DD&end=YYYY-MM-DD (local, inclusive).
    ytd starts at local midnight on 1 January of the station's current year."""
    now = timezone.now()
    preset = request.GET.get('range')
    if preset in RANGE_PRESETS:
        return now - RANGE_PRESETS[preset], now
    if preset == 'ytd':
        jan1 = now.astimezone(station.tzinfo).date().replace(month=1, day=1)
        return charts.local_range(station, jan1, jan1)[0], now
    if preset == 'all':
        # Daily rollups cover the whole history even after old raw rows are downsampled.
        first_day = DailyRollup.objects.filter(station=station).order_by('date').values_list('date', flat=True).first()
        if first_day is None:
            return now - RANGE_PRESETS['7d'], now
        return charts.local_range(station, first_day, first_day)[0], now
    try:
        start = dt.date.fromisoformat(request.GET['start'])
        end = dt.date.fromisoformat(request.GET.get('end') or request.GET['start'])
    except (KeyError, ValueError):
        return now - RANGE_PRESETS['7d'], now
    if end < start:
        start, end = end, start
    return charts.local_range(station, start, end)


def station_charts(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    stations = list(visible_stations(request.user))
    return render(request, 'weather/charts.html', {
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'years': charts.years_available(station),
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'charts',
    })


def station_chart_data(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        raise Http404
    prefs = prefs_for_request(request)
    kind = request.GET.get('kind', 'history')
    meta = {'units': prefs.as_json(), 'tz': station.timezone}
    if kind == 'history':
        start, end = _parse_range(request, station)
        if request.GET.get('format') == 'csv':
            response = HttpResponse(charts.history_csv(station, start, end, prefs), content_type='text/csv; charset=utf-8')
            name = f"{station.slug}-{start.astimezone(station.tzinfo):%Y%m%d}-{end.astimezone(station.tzinfo):%Y%m%d}.csv"
            response['Content-Disposition'] = f'attachment; filename="{name}"'
            return response
        data = charts.history(station, start, end, prefs)
    elif kind == 'rose':
        start, end = _parse_range(request, station)
        data = charts.wind_rose(station, start, end, prefs)
    elif kind == 'calendar':
        try:
            year = int(request.GET.get('year', timezone.now().astimezone(station.tzinfo).year))
        except ValueError:
            return HttpResponseBadRequest('bad year')
        data = charts.calendar(station, year, request.GET.get('metric', 'high'), prefs)
    elif kind == 'yoy':
        data = charts.year_over_year(station, request.GET.get('metric', 'rain'), prefs)
    else:
        return HttpResponseBadRequest('unknown kind')
    response = JsonResponse({**meta, **data})
    response['Cache-Control'] = 'private, max-age=60'
    return response


# ── Almanac ───────────────────────────────────────────────────────────────────

def _mean_int(values):
    return round(sum(values) / len(values)) if values else None


def station_almanac(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    tz = station.tzinfo
    today = timezone.now().astimezone(tz).date()
    try:
        picked = dt.date.fromisoformat(request.GET['date'])
    except (KeyError, ValueError):
        picked = today
    years = charts.years_available(station)
    scope = request.GET.get('records', 'all')
    record_year = int(scope) if scope.isdigit() and int(scope) in years else None
    hard_freeze = request.GET.get('frost') == 'freeze'
    threshold = almanac.HARD_FREEZE_C if hard_freeze else almanac.FROST_C
    seasons = almanac.frost_dates(station, threshold)
    stations = list(visible_stations(request.user))
    return render(request, 'weather/almanac.html', {
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'almanac',
        'picked': picked,
        'is_today': picked.month == today.month and picked.day == today.day,
        'day': almanac.on_this_day(station, picked.month, picked.day),
        'records': almanac.records(station, record_year),
        'record_scope': record_year or 'all',
        'years': years,
        'seasons': list(reversed(seasons)),
        'frost_summary': almanac.frost_summary(seasons),
        'avg_season': _mean_int([s.season_days for s in seasons if s.season_days and not (s.spring_uncertain or s.fall_uncertain)]),
        'hard_freeze': hard_freeze,
        'threshold_c': threshold,
    })


# ── Data quality ──────────────────────────────────────────────────────────────

@login_required
def station_quality(request, slug):
    station = _owned_station(request, slug)
    if request.method == 'POST':
        form = ExclusionForm(request.POST, station=station)
        if form.is_valid():
            exclusion = form.save(request.user)
            quality.launch(exclusion, 'apply')
            messages.success(request, 'Exclusion added. The readings are being set aside now; charts and records '
                                      'update within about five minutes.')
            return redirect('weather:station-quality', slug=station.slug)
    else:
        initial = {}
        tz = station.tzinfo
        try:
            day_start = dt.date.fromisoformat(request.GET['start'])
            day_end = dt.date.fromisoformat(request.GET.get('end') or request.GET['start'])
            initial['start'] = dt.datetime.combine(day_start, dt.time(), tzinfo=tz)
            initial['end'] = dt.datetime.combine(day_end + dt.timedelta(days=1), dt.time(), tzinfo=tz)
        except (KeyError, ValueError):
            pass
        if request.GET.get('group') in quality.EXCLUSION_GROUPS:
            initial['groups'] = [request.GET['group']]
        if request.GET.get('reason'):
            initial['reason'] = request.GET['reason'][:200]
        form = ExclusionForm(initial=initial, station=station)
    exclusions = station.exclusions.select_related('created_by')
    return render(request, 'weather/station_quality.html', {
        'station': station, 'form': form, 'tab': 'quality', 'exclusions': exclusions,
        'busy': any(e.status in ('pending', 'removing') for e in exclusions),
    })


@login_required
@require_POST
def station_quality_remove(request, slug, pk):
    station = _owned_station(request, slug)
    exclusion = get_object_or_404(DataExclusion, pk=pk, station=station)
    if exclusion.status == 'removing':
        return redirect('weather:station-quality', slug=station.slug)
    DataExclusion.objects.filter(pk=exclusion.pk).update(status='removing')
    quality.launch(exclusion, 'remove')
    messages.success(request, 'Restoring the original readings. Charts and records update within about five minutes.')
    return redirect('weather:station-quality', slug=station.slug)


# ── Reports ───────────────────────────────────────────────────────────────────

REPORT_PRESETS = [('this_month', 'This month'), ('last_month', 'Last month'), ('this_year', 'This year'),
                  ('last_year', 'Last year'), ('all', 'All')]


def _report_range(request, station):
    today = timezone.now().astimezone(station.tzinfo).date()
    preset = request.GET.get('period', 'this_month')
    first_of_month = today.replace(day=1)
    if preset == 'last_month':
        end = first_of_month - dt.timedelta(days=1)
        return preset, end.replace(day=1), end
    if preset == 'this_year':
        return preset, today.replace(month=1, day=1), today
    if preset == 'last_year':
        return preset, dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
    if preset == 'all':
        first = DailyRollup.objects.filter(station=station).order_by('date').values_list('date', flat=True).first()
        return preset, first or today, today
    if preset == 'custom':
        try:
            start = dt.date.fromisoformat(request.GET['start'])
            end = dt.date.fromisoformat(request.GET.get('end') or request.GET['start'])
            return preset, min(start, end), max(start, end)
        except (KeyError, ValueError):
            pass
    return 'this_month', first_of_month, today


def station_reports(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    prefs = prefs_for_request(request)
    preset, start, end = _report_range(request, station)
    group = request.GET.get('group', 'auto')
    columns = request.GET.getlist('col') or reports.DEFAULT_COLUMNS
    report = reports.build(station, start, end, prefs, group, columns)
    if request.GET.get('format') == 'csv':
        response = HttpResponse(reports.to_csv(report), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = (f'attachment; filename="{station.slug}-{report.group}-report-'
                                           f'{start:%Y%m%d}-{end:%Y%m%d}.csv"')
        return response
    stations = list(visible_stations(request.user))
    query = request.GET.copy()
    query['format'] = 'csv'
    cells = [[(v, reports.digits_for(c, prefs)) for v, c in zip(row.values, report.columns)] for row in report.rows]
    return render(request, 'weather/reports.html', {
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'reports',
        'report': report,
        'rows': list(zip(report.rows, cells)),
        'summary': [(v, reports.digits_for(c, prefs)) for v, c in zip(report.summary, report.columns)],
        'headers': [(c, reports.unit_for(c, prefs)) for c in report.columns],
        'all_columns': reports.COLUMNS,
        'selected': set(columns),
        'presets': REPORT_PRESETS,
        'preset': preset,
        'group_choice': group if group in ('day', 'month', 'year') else 'auto',
        'csv_query': query.urlencode(),
    })


# ── Growing ───────────────────────────────────────────────────────────────────

def station_growing(request, slug):
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    prefs = prefs_for_request(request)
    tz = station.tzinfo
    today = timezone.now().astimezone(tz).date()
    imperial = prefs.temp == 'F'
    deg = 1.8 if imperial else 1.0                      # °C·days → viewer degree-days

    # Growing degree days
    preset = agro.GDD_BY_KEY.get(request.GET.get('crop'), agro.GDD_PRESETS[0])
    base_c, cap_c = agro.f_to_c(preset.base_f), (agro.f_to_c(preset.cap_f) if preset.cap_f else None)
    gdd = agro.gdd_seasons(station, base_c, cap_c, preset.start, preset.end, today)
    current = next((s for s in gdd if s['year'] == today.year), None)
    index = (min(today, dt.date(today.year, *preset.end)) - dt.date(today.year, *preset.start)).days
    avg, avg_years = agro.average_to_date(gdd, index, today.year) if index >= 0 else (None, 0)

    # Chill
    model = request.GET.get('chill') if request.GET.get('chill') in agro.CHILL_MODELS else 'hours'
    chill = agro.chill_seasons(station, model, today)

    # Evapotranspiration: last 30 days daily, this year by month.
    recent = agro.et0_days(station, today - dt.timedelta(days=29), today)
    year_days = agro.et0_days(station, today.replace(month=1, day=1), today)
    months = {}
    for d, et0, method, rain in year_days:
        m = months.setdefault(d.month, {'month': d.replace(day=1), 'et0': 0.0, 'hg': 0.0, 'rain': 0.0, 'days': 0, 'hg_days': 0})
        if et0 is not None:
            m['et0'] += et0
            m['days'] += 1
        if rain is not None:
            m['rain'] += rain
    # Hargreaves alongside, for days with full temperature data (siting-independent).
    for r in DailyRollup.objects.filter(station=station, date__gte=today.replace(month=1, day=1), date__lte=today,
                                        temp_coverage__gte=agro.DAY_COVERAGE).exclude(temp_max_c__isnull=True):
        if station.latitude is not None:
            months[r.date.month]['hg'] += agro.et0_hargreaves(r.temp_max_c, r.temp_min_c, float(station.latitude), r.date.timetuple().tm_yday)
            months[r.date.month]['hg_days'] += 1
    last7 = [x for x in recent[-7:] if x[1] is not None]
    week_et0 = sum(x[1] for x in last7)
    week_rain = sum(x[3] or 0 for x in recent[-7:])

    rain_digits = prefs.digits['rain'] + (1 if prefs.rain == 'in' else 0)
    chart_data = {
        'units': prefs.as_json(),
        'deg_label': f'{prefs.label("temp")}·days',
        'gdd': [{'year': s['year'], 'values': [None if v is None else round(v * deg) for v in s['values']]} for s in gdd],
        'gdd_start': list(preset.start),
        'chill': [{'label': s['label'], 'values': s['values']} for s in chill],
        'chill_start': [5, 1] if (station.latitude is not None and station.latitude < 0) else [11, 1],
        'chill_label': agro.CHILL_MODELS[model][0],
        'chill_unit': 'units' if model == 'utah' else 'hours',
        'et0': [[d.isoformat(), None if e is None else round(prefs.r(e), rain_digits), None if r is None else round(prefs.r(r), rain_digits), m]
                for d, e, m, r in recent],
    }
    stations = list(visible_stations(request.user))
    return render(request, 'weather/growing.html', {
        'station': station, 'tab': 'growing',
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'presets': agro.GDD_PRESETS, 'preset': preset,
        'season_label': f"{dt.date(2001, *preset.start):%b} {preset.start[1]} – {dt.date(2001, *preset.end):%b} {preset.end[1]}",
        'gdd_total': None if current is None else current['total'] * deg,
        'gdd_missing': current['missing'] if current else 0,
        'gdd_avg': None if avg is None else avg * deg, 'gdd_avg_years': avg_years,
        'gdd_delta': (current['total'] - avg) * deg if (current is not None and avg is not None) else None,
        'gdd_seasons': [{'year': s['year'], 'total': s['total'] * deg, 'missing': s['missing'], 'complete': s['complete']} for s in reversed(gdd)],
        'chill_models': [(k, v[0]) for k, v in agro.CHILL_MODELS.items()], 'chill_model': model,
        'chill_seasons': list(reversed(chill)),
        'week_et0': week_et0, 'week_rain': week_rain, 'week_days': len(last7), 'week_balance': week_rain - week_et0,
        'et0_months': [months[k] for k in sorted(months)],
        'chart_data': chart_data,
        'has_location': station.latitude is not None,
    })
