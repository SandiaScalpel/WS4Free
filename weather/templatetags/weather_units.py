"""Display formatting in the viewer's units.

Every filter takes the request's UnitPrefs (the `units` context variable, set
by weather.context_processors) as its argument and renders '—' for missing data:

    {{ latest.data.temp_c|temp:units }}        → 58.8°F
    {{ latest.data.temp_c|temp_value:units }}  → 58.8   (unit rendered separately)
"""
from django import template

from weather import units as u

register = template.Library()
DASH = '—'


def _prefs(prefs):
    return prefs if isinstance(prefs, u.UnitPrefs) else u.default_prefs()


def _missing(value):
    return value is None or value == ''


def _fmt(value, digits):
    return f'{value:,.{digits}f}'


@register.filter
def temp(celsius, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(celsius) else f'{_fmt(p.t(celsius), p.digits["temp"])}{p.label("temp")}'


@register.filter
def temp_value(celsius, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(celsius) else _fmt(p.t(celsius), p.digits['temp'])


@register.filter
def temp_round(celsius, prefs=None):
    """Whole degrees with a bare degree sign — for compact highs/lows."""
    p = _prefs(prefs)
    return DASH if _missing(celsius) else f'{p.t(celsius):.0f}°'


@register.filter
def speed(ms, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(ms) else f'{_fmt(p.w(ms), p.digits["wind"])} {p.label("wind")}'


@register.filter
def speed_value(ms, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(ms) else _fmt(p.w(ms), p.digits['wind'])


@register.filter
def pressure(hpa, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(hpa) else f'{_fmt(p.p(hpa), p.digits["pressure"])} {p.label("pressure")}'


@register.filter
def pressure_value(hpa, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(hpa) else _fmt(p.p(hpa), p.digits['pressure'])


@register.filter
def pressure_change(hpa, prefs=None):
    p = _prefs(prefs)
    if _missing(hpa):
        return DASH
    value = p.p(hpa)
    return f'{value:+.{p.digits["pressure"]}f} {p.label("pressure")}'


@register.filter
def rain(mm, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(mm) else f'{_fmt(p.r(mm), p.digits["rain"])} {p.label("rain")}'


@register.filter
def rain_value(mm, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(mm) else _fmt(p.r(mm), p.digits['rain'])


@register.filter
def rain_rate(mmh, prefs=None):
    p = _prefs(prefs)
    return DASH if _missing(mmh) else f'{_fmt(p.r(mmh), p.digits["rain"])} {p.label("rain")}/h'


@register.filter
def pct(value, prefs=None):
    return DASH if _missing(value) else f'{value:.0f}%'


@register.filter
def whole(value, prefs=None):
    return DASH if _missing(value) else f'{value:,.0f}'


@register.filter
def compass(degrees):
    return u.compass(degrees)


@register.filter
def unit_label(prefs, quantity):
    return _prefs(prefs).label(quantity)


@register.filter
def record_value(record, prefs=None):
    """Format an almanac Record by its kind."""
    p = _prefs(prefs)
    v = record.value
    return {
        'temp': lambda: temp(v, p),
        'rain': lambda: rain(v, p),
        'rate': lambda: rain_rate(v, p),
        'speed': lambda: speed(v, p),
        'pressure': lambda: pressure(v, p),
        'uv': lambda: whole(v),
        'days': lambda: f'{v} days',
        'pm': lambda: f'{v:.1f} µg/m³',
        'ppm': lambda: f'{v:.0f} ppm',
        'strikes': lambda: f'{v:.0f} strikes',
    }[record.kind]()


@register.filter
def frost_threshold(celsius, prefs=None):
    p = _prefs(prefs)
    return f'{p.t(celsius):.0f}{p.label("temp")}'


_RECORD_GROUP = {'temp': 'temp', 'rain': 'rain', 'rate': 'rain', 'speed': 'wind', 'pressure': 'pressure', 'uv': 'solar'}


@register.filter
def record_group(record):
    """The data-quality group an almanac record comes from."""
    if getattr(record, 'sensor', ''):
        return f'x:{record.sensor}'
    return _RECORD_GROUP.get(record.kind, 'temp')


@register.filter
def num(cell):
    """(value, digits) → formatted number or a dash."""
    value, digits = cell
    return DASH if value is None else f'{value:,.{digits}f}'


@register.filter
def temp_delta(celsius, prefs=None):
    """A temperature difference, signed: 2.5 °C → '+4.5°F'."""
    p = _prefs(prefs)
    return DASH if _missing(celsius) else f'{p.t_delta(celsius):+.1f}{p.label("temp")}'


@register.filter
def month_name(number):
    import calendar
    return calendar.month_abbr[int(number)]


@register.filter
def get_item(mapping, key):
    return (mapping or {}).get(str(key))


@register.filter
def noon_offset(coef):
    """Total correction at a sunny noon (~900 W/m²), °C."""
    return coef.get('night', 0) + coef.get('day', 0) + coef.get('solar', 0) * 0.9


@register.simple_tag
def help_url(tab):
    """Guide page that explains a station tab (weather.help.TAB_PAGES)."""
    from django.urls import reverse

    from ..help import TAB_PAGES
    return reverse('weather:help-page', args=[TAB_PAGES.get(tab, 'dashboard')])
