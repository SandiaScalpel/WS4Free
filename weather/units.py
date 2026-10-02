"""Unit conversions. Storage is SI: °C, hPa, m/s, mm, mm/h, W/m².

Ingest converts the imperial values consoles send into SI with the
f_to_c / mph_to_ms / inhg_to_hpa / in_to_mm helpers. Display goes the other
way through `UnitPrefs`, resolved per request (`prefs_for_request`): a signed-in
user's own choices, else a visitor's imperial/metric cookie, else the site
default (DEFAULT_UNIT_SYSTEM).
"""
import math
from dataclasses import asdict, dataclass

from django.conf import settings

MPH_TO_MS = 0.44704
INHG_TO_HPA = 33.8638866667
IN_TO_MM = 25.4
KN_TO_MS = 0.514444
MMHG_TO_HPA = 1.33322387415
FEET_PER_METER = 3.28084


def f_to_c(f):
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def c_to_f(c):
    return None if c is None else c * 9.0 / 5.0 + 32.0


def mph_to_ms(mph):
    return None if mph is None else mph * MPH_TO_MS


def inhg_to_hpa(inhg):
    return None if inhg is None else inhg * INHG_TO_HPA


def in_to_mm(inches):
    return None if inches is None else inches * IN_TO_MM


def dewpoint_c(temp_c, humidity):
    """Magnus–Tetens dew point (Alduchov & Eskridge coefficients), °C.

    Used only when the console does not send a dew point. Accurate to well under
    0.5 °C over normal weather ranges.
    """
    if temp_c is None or humidity is None or humidity <= 0:
        return None
    a, b = 17.625, 243.04
    gamma = math.log(humidity / 100.0) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)


WIND_CHILL, HEAT_INDEX = 'wind_chill', 'heat_index'


def feels_like_c(temp_c, humidity, wind_ms):
    """Apparent temperature, the US National Weather Service way: heat index at
    80 °F and above, wind chill at 50 °F and below with wind of at least 3 mph,
    otherwise the air temperature."""
    return apparent(temp_c, humidity, wind_ms)[0]


def apparent(temp_c, humidity, wind_ms):
    """(feels-like °C, which index applied: WIND_CHILL, HEAT_INDEX or None)."""
    if temp_c is None:
        return None, None
    t = c_to_f(temp_c)
    if t >= 80 and humidity is not None:
        rh = humidity
        hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
        if (hi + t) / 2 >= 80:
            hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 0.00683783 * t * t
                  - 0.05481717 * rh * rh + 0.00122874 * t * t * rh + 0.00085282 * t * rh * rh
                  - 0.00000199 * t * t * rh * rh)
            if rh < 13 and 80 <= t <= 112:
                hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
            elif rh > 85 and 80 <= t <= 87:
                hi += ((rh - 85) / 10) * ((87 - t) / 5)
        return f_to_c(hi), HEAT_INDEX
    mph = (wind_ms or 0) / MPH_TO_MS
    if t <= 50 and mph >= 3:
        wc = 35.74 + 0.6215 * t - 35.75 * mph ** 0.16 + 0.4275 * t * mph ** 0.16
        return f_to_c(wc), WIND_CHILL
    return temp_c, None


COMPASS_POINTS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                  'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')


def compass(degrees):
    if degrees is None:
        return ''
    return COMPASS_POINTS[int((degrees % 360) / 22.5 + 0.5) % 16]


# WHO UV index categories: (upper bound inclusive, label, status token).
UV_CATEGORIES = ((2.99, 'Low', 'good'), (5.99, 'Moderate', 'warn'), (7.99, 'High', 'warm'),
                 (10.99, 'Very high', 'bad'), (math.inf, 'Extreme', 'bad'))


def uv_category(uv):
    if uv is None:
        return None
    for upper, label, tone in UV_CATEGORIES:
        if uv <= upper:
            return {'label': label, 'tone': tone}


def pressure_trend(change_hpa):
    """Three-hour pressure tendency in words (WMO-style thresholds)."""
    if change_hpa is None:
        return None
    if abs(change_hpa) < 1.0:
        return {'label': 'Steady', 'direction': 0}
    fast = abs(change_hpa) >= 3.5
    if change_hpa > 0:
        return {'label': 'Rising fast' if fast else 'Rising', 'direction': 1}
    return {'label': 'Falling fast' if fast else 'Falling', 'direction': -1}


# ── Display preferences ──────────────────────────────────────────────────────

UNIT_CHOICES = {
    'temp': [('F', '°F'), ('C', '°C')],
    'wind': [('mph', 'mph'), ('kmh', 'km/h'), ('ms', 'm/s'), ('kn', 'knots')],
    'pressure': [('inhg', 'inHg'), ('hpa', 'hPa'), ('mmhg', 'mmHg')],
    'rain': [('in', 'in'), ('mm', 'mm')],
}
SYSTEMS = {
    'imperial': {'temp': 'F', 'wind': 'mph', 'pressure': 'inhg', 'rain': 'in'},
    'metric': {'temp': 'C', 'wind': 'kmh', 'pressure': 'hpa', 'rain': 'mm'},
}
UNITS_COOKIE = 'ws4f_units'


@dataclass(frozen=True)
class UnitPrefs:
    temp: str = 'F'
    wind: str = 'mph'
    pressure: str = 'inhg'
    rain: str = 'in'

    @classmethod
    def for_system(cls, system):
        return cls(**SYSTEMS.get(system, SYSTEMS['imperial']))

    @property
    def system(self):
        for name, units in SYSTEMS.items():
            if asdict(self) == units:
                return name
        return 'custom'

    def label(self, quantity):
        return dict(UNIT_CHOICES[quantity])[getattr(self, quantity)]

    # Conversions from SI. Each returns None for None.
    def t(self, c):
        return None if c is None else (c_to_f(c) if self.temp == 'F' else c)

    def t_delta(self, c):
        return None if c is None else (c * 9 / 5 if self.temp == 'F' else c)

    def w(self, ms):
        if ms is None:
            return None
        return {'mph': ms / MPH_TO_MS, 'kmh': ms * 3.6, 'ms': ms, 'kn': ms / KN_TO_MS}[self.wind]

    def p(self, hpa):
        if hpa is None:
            return None
        return {'inhg': hpa / INHG_TO_HPA, 'hpa': hpa, 'mmhg': hpa / MMHG_TO_HPA}[self.pressure]

    def r(self, mm):
        if mm is None:
            return None
        return mm / IN_TO_MM if self.rain == 'in' else mm

    # Decimal places that suit each unit's typical resolution.
    @property
    def digits(self):
        return {
            'temp': 1,
            'wind': 0 if self.wind == 'kmh' else 1,
            'pressure': 2 if self.pressure == 'inhg' else 1,
            'rain': 2 if self.rain == 'in' else 1,
        }

    def as_json(self):
        return {**asdict(self), 'labels': {q: self.label(q) for q in UNIT_CHOICES}, 'digits': self.digits}


def default_prefs():
    return UnitPrefs.for_system(settings.DEFAULT_UNIT_SYSTEM)


def prefs_for_request(request):
    user = getattr(request, 'user', None)
    base = default_prefs()
    if user is not None and user.is_authenticated:
        profile = getattr(user, 'profile', None)
        if profile is not None:
            chosen = {}
            for quantity in UNIT_CHOICES:
                value = getattr(profile, f'unit_{quantity}', '')
                if value in dict(UNIT_CHOICES[quantity]):
                    chosen[quantity] = value
            return UnitPrefs(**{**asdict(base), **chosen})
        return base
    system = request.COOKIES.get(UNITS_COOKIE)
    return UnitPrefs.for_system(system) if system in SYSTEMS else base
