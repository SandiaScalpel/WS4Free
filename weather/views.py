import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import redirect_to_login
from django.db.models import Max, Min, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import agro, almanac, charts, dashboard, reports
from . import forecast as forecasts
from . import neighbours as neighbour_data
from . import calibration, quality
from . import compare as compare_data
from . import help as help_docs
from .forms import CalibrationForm, ExclusionForm, StationCreateForm, SiteSettingsForm, StationEventForm, StationSettingsForm
from .ingest.store import mark_dirty
from .models import DailyRollup, DataExclusion, Neighbour, Observation, SiteSettings, Station, StationEvent, TempCalibration
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


def station_list(request):
    """Every station the viewer can see (header 'Stations' link)."""
    stations = list(visible_stations(request.user).select_related('latest'))
    return render(request, 'weather/home.html', {'stations': stations, 'is_list': True})


@login_required
def station_create(request):
    """Add a station (site administrators)."""
    if not request.user.is_staff:
        raise Http404
    form = StationCreateForm(request.POST or None, initial={'source': Station.SOURCE_AMBIENT, 'timezone': settings.TIME_ZONE})
    if request.method == 'POST' and form.is_valid():
        station = form.save(request.user)
        messages.success(request, f'{station.name} added. Now point its console (or WeeWX) at WS4Free.')
        return redirect('weather:station-setup', slug=station.slug)
    return render(request, 'weather/station_create.html', {
        'form': form, 'timezones': StationSettingsForm.timezone_choices(),
    })


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


def station_forecast(request, slug):
    """Every day of the forecast (the dashboard's forecast card links here)."""
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        return redirect_to_login(request.get_full_path())
    prefs = prefs_for_request(request)
    today = timezone.now().astimezone(station.tzinfo).date()
    days = forecasts.temperature_bars(forecasts.display(forecasts.current(station), prefs, today))
    stations = list(visible_stations(request.user))
    return render(request, 'weather/forecast.html', {
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'forecast',
        'forecast': days,
    })


def station_forecast_day(request, slug, date):
    """One day of the forecast hour by hour: the pop-up dialog's contents (htmx)."""
    try:
        station = _viewable_station(request, slug)
    except _LoginRequired:
        raise Http404
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise Http404
    prefs = prefs_for_request(request)
    now_local = timezone.now().astimezone(station.tzinfo)
    found, hours = forecasts.day(station, date)
    if found is None:
        raise Http404
    all_days = forecasts.current(station)            # already fresh: day() just refreshed it if due
    dates = [d['date'] for d in all_days]
    index = dates.index(date)
    hours = forecasts.display_hours(hours, prefs, now_local.replace(tzinfo=None))
    return render(request, 'weather/partials/forecast_day.html', {
        'station': station,
        'day': forecasts.display([found], prefs, now_local.date())[0],
        'hours': hours,
        'chart': forecasts.hours_chart(hours, prefs),
        'prev_date': dates[index - 1] if index > 0 else None,
        'next_date': dates[index + 1] if index + 1 < len(dates) else None,
    })


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

ALL_TIME_START = dt.datetime(1900, 1, 1, tzinfo=dt.UTC)

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
        'calibration_notes': calibration.overlapping(station, ALL_TIME_START, calibration.FAR_FUTURE),
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
    owner = request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk)
    if kind == 'history':
        start, end = _parse_range(request, station)
        if request.GET.get('format') == 'csv':
            response = HttpResponse(charts.history_csv(station, start, end, prefs, include_private=owner),
                                    content_type='text/csv; charset=utf-8')
            name = f"{station.slug}-{start.astimezone(station.tzinfo):%Y%m%d}-{end.astimezone(station.tzinfo):%Y%m%d}.csv"
            response['Content-Disposition'] = f'attachment; filename="{name}"'
            return response
        data = charts.history(station, start, end, prefs, include_private=owner)
        data['events'] = charts.events(station, start, end, include_private=owner)
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
        'calibration_notes': calibration.overlapping(station, ALL_TIME_START, calibration.FAR_FUTURE),
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'almanac',
        'picked': picked,
        'is_today': picked.month == today.month and picked.day == today.day,
        'day': almanac.on_this_day(station, picked.month, picked.day),
        'records': almanac.records(station, record_year),
        'sensor_records': almanac.sensor_records(station, record_year, include_private=request.user.is_authenticated and (
            request.user.is_staff or station.owner_id == request.user.pk)),
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
    prefs = prefs_for_request(request)
    form = cal_form = None
    if request.method == 'POST' and request.POST.get('form') == 'calibration':
        cal_form = CalibrationForm(request.POST, station=station, prefs=prefs, prefix='cal')
        if cal_form.is_valid():
            cal = cal_form.save(request.user)
            if cal.mode == 'reference':
                calibration.launch(cal, 'fit')
                messages.success(request, f'Fetching {cal.reference_station} data and fitting the correction. '
                                          'This takes a few minutes; review it here before applying.')
            else:
                calibration.launch(cal, 'apply')
                messages.success(request, 'Manual calibration added. Readings are being corrected now; charts and '
                                          'records update within about five minutes.')
            return redirect(f"{reverse('weather:station-quality', args=[station.slug])}#calibration")
    elif request.method == 'POST':
        form = ExclusionForm(request.POST, station=station)
        if form.is_valid():
            exclusion = form.save(request.user)
            quality.launch(exclusion, 'apply')
            messages.success(request, 'Exclusion added. The readings are being set aside now; charts and records '
                                      'update within about five minutes.')
            return redirect('weather:station-quality', slug=station.slug)
    if form is None:
        initial = {}
        tz = station.tzinfo
        try:
            day_start = dt.date.fromisoformat(request.GET['start'])
            day_end = dt.date.fromisoformat(request.GET.get('end') or request.GET['start'])
            initial['start'] = dt.datetime.combine(day_start, dt.time(), tzinfo=tz)
            initial['end'] = dt.datetime.combine(day_end + dt.timedelta(days=1), dt.time(), tzinfo=tz)
        except (KeyError, ValueError):
            pass
        group = request.GET.get('group', '')
        if group in quality.EXCLUSION_GROUPS or (group.startswith('x:') and group[2:] in (station.sensors or {})):
            initial['groups'] = [group]
        if request.GET.get('reason'):
            initial['reason'] = request.GET['reason'][:200]
        form = ExclusionForm(initial=initial, station=station)
    if cal_form is None:
        cal_form = CalibrationForm(station=station, prefs=prefs, prefix='cal')
    exclusions = station.exclusions.select_related('created_by')
    calibrations = list(station.calibrations.select_related('created_by'))
    return render(request, 'weather/station_quality.html', {
        'station': station, 'form': form, 'cal_form': cal_form, 'tab': 'quality', 'exclusions': exclusions,
        'calibrations': calibrations,
        'busy': any(e.status in ('pending', 'removing') for e in exclusions)
                or any(c.status in ('fitting', 'pending', 'removing') for c in calibrations),
    })


@login_required
@require_POST
def station_calibration_apply(request, slug, pk):
    station = _owned_station(request, slug)
    cal = get_object_or_404(TempCalibration, pk=pk, station=station)
    if TempCalibration.objects.filter(pk=cal.pk, status='fitted').update(status='pending'):
        calibration.launch(cal, 'apply')
        messages.success(request, 'Applying the correction. Charts and records update within about five minutes.')
    return redirect(f"{reverse('weather:station-quality', args=[station.slug])}#calibration")


@login_required
@require_POST
def station_calibration_remove(request, slug, pk):
    station = _owned_station(request, slug)
    cal = get_object_or_404(TempCalibration, pk=pk, station=station)
    if cal.status in ('fitting', 'pending', 'removing'):
        return redirect(f"{reverse('weather:station-quality', args=[station.slug])}#calibration")
    if cal.originals.exists():
        TempCalibration.objects.filter(pk=cal.pk).update(status='removing')
        calibration.launch(cal, 'remove')
        messages.success(request, 'Restoring the original readings. Charts and records update within about five minutes.')
    else:
        cal.delete()                     # never applied (reviewed and discarded, or a failed fit)
        messages.success(request, 'Calibration discarded.')
    return redirect(f"{reverse('weather:station-quality', args=[station.slug])}#calibration")


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
    owner = request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk)
    report = reports.build(station, start, end, prefs, group, columns, include_private=owner)
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
        'calibration_notes': calibration.overlapping(station, ALL_TIME_START, calibration.FAR_FUTURE),
        'station': station,
        'other_stations': [s for s in stations if s.pk != station.pk],
        'can_manage': request.user.is_authenticated and (request.user.is_staff or station.owner_id == request.user.pk),
        'tab': 'reports',
        'report': report,
        'rows': list(zip(report.rows, cells)),
        'summary': [(v, reports.digits_for(c, prefs)) for v, c in zip(report.summary, report.columns)],
        'headers': [(c, reports.unit_for(c, prefs)) for c in report.columns],
        'all_columns': reports.COLUMNS,
        'sensor_columns': reports.sensor_columns(station, include_private=owner),
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
        'calibration_notes': calibration.overlapping(station, ALL_TIME_START, calibration.FAR_FUTURE),
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


# ── User guide ────────────────────────────────────────────────────────────────

def help_page(request, slug='index'):
    """The user guide (docs/guide/*.md) and changelog, readable by anyone: it
    describes the software, never a particular station."""
    if slug == 'index' and request.resolver_match.url_name == 'help-page':
        return redirect('weather:help', permanent=True)
    rendered = help_docs.page(slug)
    if rendered is None:
        raise Http404('No such help page')
    title, html, sections = rendered
    slugs = [s for s, _ in help_docs.PAGES]
    i = slugs.index(slug)
    return render(request, 'weather/help.html', {
        'title': title, 'body': html, 'sections': sections, 'slug': slug, 'pages': help_docs.PAGES,
        'previous': help_docs.PAGES[i - 1] if i > 0 else None,
        'next': help_docs.PAGES[i + 1] if i + 1 < len(slugs) else None,
    })


# ── Station comparison ────────────────────────────────────────────────────────

def _compare_selection(request):
    """(all stations the viewer can see, the chosen ones in the order chosen)."""
    available = list(visible_stations(request.user))
    by_slug = {s.slug: s for s in available}
    chosen = []
    for slug in request.GET.getlist('s'):
        if slug in by_slug and by_slug[slug] not in chosen:
            chosen.append(by_slug[slug])
    return available, chosen[:compare_data.MAX_STATIONS]


def compare_page(request):
    available, chosen = _compare_selection(request)
    if len(chosen) < 2:
        # Start with two: what was asked for (a station's Compare link), else the site's
        # main station, then the next ones.
        default = _home_station(request, available)
        for candidate in [default] + available:
            if len(chosen) >= 2:
                break
            if candidate is not None and candidate not in chosen:
                chosen.append(candidate)
    return render(request, 'weather/compare.html', {
        'available': available, 'chosen': [s.slug for s in chosen],
        'available_json': [{'slug': s.slug, 'name': s.name} for s in available],
        'max_stations': compare_data.MAX_STATIONS,
    })


def compare_json(request):
    _, chosen = _compare_selection(request)
    if not chosen:
        return HttpResponseBadRequest('choose at least one station')
    prefs = prefs_for_request(request)
    start, end = _parse_range(request, chosen[0])
    if request.GET.get('range') == 'all':
        # Everything any of the chosen stations has recorded.
        firsts = [DailyRollup.objects.filter(station=s).order_by('date').values_list('date', flat=True).first()
                  for s in chosen]
        firsts = [(s, d) for s, d in zip(chosen, firsts) if d is not None]
        if firsts:
            start = min(charts.local_range(s, d, d)[0] for s, d in firsts)
    data = compare_data.compare(chosen, start, end, prefs)
    response = JsonResponse({'units': prefs.as_json(), 'tz': chosen[0].timezone, **data})
    response['Cache-Control'] = 'private, max-age=60'
    return response


# ── Station log ───────────────────────────────────────────────────────────────

@login_required
def station_log(request, slug, pk=None):
    """List the station's log, and add or edit an entry (?edit=<pk> pre-fills the form)."""
    station = _owned_station(request, slug)
    instance = None
    edit_pk = pk or request.GET.get('edit')
    if edit_pk:
        instance = get_object_or_404(StationEvent, pk=edit_pk, station=station)
    form = StationEventForm(request.POST or None, instance=instance, station=station)
    if request.method == 'POST' and form.is_valid():
        form.save(request.user)
        messages.success(request, 'Log entry saved.' if instance else 'Log entry added.')
        return redirect('weather:station-log', slug=station.slug)
    if instance is None and not request.POST:
        form.initial.setdefault('date', timezone.now().astimezone(station.tzinfo).date())
    return render(request, 'weather/station_log.html', {
        'station': station, 'tab': 'log', 'form': form, 'editing': instance,
        'events': station.events.all(),
    })


@login_required
@require_POST
def station_log_delete(request, slug, pk):
    station = _owned_station(request, slug)
    get_object_or_404(StationEvent, pk=pk, station=station).delete()
    messages.success(request, 'Log entry deleted.')
    return redirect('weather:station-log', slug=station.slug)


# ── Neighbouring stations ─────────────────────────────────────────────────────

@login_required
def station_neighbours(request, slug):
    """Owner's list of neighbouring Weather Underground stations, and how this
    station's temperature and humidity compare with theirs."""
    station = _owned_station(request, slug)
    if request.method == 'POST':
        try:
            added = neighbour_data.add(station, request.POST.get('wu_id', ''))
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'{added.wu_id} added.')
        return redirect('weather:station-neighbours', slug=station.slug)

    prefs = prefs_for_request(request)
    now = timezone.now()
    range_key = request.GET.get('range') if request.GET.get('range') in neighbour_data.RANGES else '7d'
    start = now - dt.timedelta(days=neighbour_data.RANGES[range_key])
    result = neighbour_data.compare(station, start, now)
    rows = []
    for n in station.neighbours.all():
        latest = n.readings.order_by('-timestamp').first()
        km = neighbour_data.distance_km(station, n)
        offset = result['neighbours'].get(n.pk, {})
        rows.append({
            'n': n, 'latest': latest,
            'distance': None if km is None else (km / 1.609344 if prefs.wind == 'mph' else km),
            'elevation_diff': None if n.elevation_m is None or station.elevation_m is None else (
                (n.elevation_m - station.elevation_m) * (3.28084 if prefs.temp == 'F' else 1)),
            'temp_offset': offset.get('temp_c'), 'humidity_offset': offset.get('humidity'),
        })
    latest = getattr(station, 'latest', None)
    ours_now = dashboard.live_values(station, latest)
    live = neighbour_data.live(station, now)
    if live:
        live['temp_diff'] = _diff(ours_now.get('temp_c'), live['temp_c'])
        live['humidity_diff'] = _diff(ours_now.get('humidity'), live['humidity'])
    return render(request, 'weather/station_neighbours.html', {
        'station': station, 'tab': 'neighbours',
        'key_set': bool(settings.WU_API_KEY),
        'poll_minutes': settings.WU_POLL_MINUTES,
        'neighbours': rows,
        'distance_unit': 'mi' if prefs.wind == 'mph' else 'km',
        'elevation_unit': 'ft' if prefs.temp == 'F' else 'm',
        'calls_per_day': neighbour_data.calls_per_day(),
        'daily_limit': neighbour_data.DAILY_LIMIT,
        'range_key': range_key, 'ranges': [('24h', '24 hours'), ('7d', '7 days'), ('30d', '30 days')],
        'summary': result['summary'],
        'live': live,
        'ours_now': ours_now,
        'chart': {
            'units': prefs.as_json(),
            'tz': station.timezone,
            'rows': [[r['t'].isoformat(),
                      _round(prefs.t(r['temp_c'][0])), _round(prefs.t(r['temp_c'][1])), r['temp_c'][2],
                      _round(r['humidity'][0]), _round(r['humidity'][1]), r['humidity'][2]] for r in result['rows']],
            'by_hour': {'temp_c': [_round(prefs.t_delta(v), 2) for v in result['by_hour']['temp_c']],
                        'humidity': [_round(v, 1) for v in result['by_hour']['humidity']]},
        },
    })


def _diff(a, b):
    return None if a is None or b is None else a - b


def _round(value, digits=1):
    return None if value is None else round(value, digits)


@login_required
@require_POST
def station_neighbour_include(request, slug, pk):
    station = _owned_station(request, slug)
    neighbour = get_object_or_404(Neighbour, pk=pk, station=station)
    neighbour.include = not neighbour.include
    neighbour.save(update_fields=['include'])
    messages.success(request, f'{neighbour.wu_id} is {"counted in" if neighbour.include else "left out of"} the comparison.')
    return redirect('weather:station-neighbours', slug=station.slug)


@login_required
@require_POST
def station_neighbour_delete(request, slug, pk):
    station = _owned_station(request, slug)
    neighbour = get_object_or_404(Neighbour, pk=pk, station=station)
    neighbour.delete()
    messages.success(request, f'{neighbour.wu_id} removed, with its readings.')
    return redirect('weather:station-neighbours', slug=station.slug)
