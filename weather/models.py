import hashlib
import re
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


def _new_push_token():
    return secrets.token_urlsafe(24)


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(f'{value!r} is not an IANA time zone name (e.g. America/Denver).')


_MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')


def normalize_mac(value):
    """'48-3f-da-aa-bb-cc' / '483fdaaabbcc' → '48:3F:DA:AA:BB:CC'. Returns None if not a MAC."""
    hexdigits = re.sub(r'[^0-9A-Fa-f]', '', value or '').upper()
    if len(hexdigits) != 12:
        return None
    return ':'.join(hexdigits[i:i + 2] for i in range(0, 12, 2))


def validate_mac(value):
    if not _MAC_RE.match(value or ''):
        raise ValidationError('Enter the MAC address as six hex pairs, e.g. 48:3F:DA:12:34:56.')


class Station(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stations')
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    mac_address = models.CharField(
        'MAC address', max_length=17, unique=True, validators=[validate_mac],
        help_text='As shown on the console or ambientweather.net, e.g. 48:3F:DA:12:34:56.',
    )
    place = models.CharField(max_length=100, blank=True, help_text='Shown under the name, e.g. "Boulder, Colorado".')
    timezone = models.CharField(max_length=64, default='UTC', validators=[validate_timezone],
                                help_text='IANA name, e.g. America/Denver. Defines the station\'s local day.')
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    elevation_m = models.FloatField('elevation (m)', null=True, blank=True)
    anemometer_height_m = models.FloatField(
        'anemometer height (m)', default=2.0,
        help_text='Height of the wind sensor above the ground; used to adjust wind for evapotranspiration.')
    is_public = models.BooleanField(default=False,
                                    help_text='Anyone can view this station\'s dashboard and charts without signing in.')
    archive_interval_s = models.PositiveIntegerField(
        'archive interval (s)', default=300,
        help_text='Pushed readings are merged into buckets of this length. 300 matches ambientweather.net.',
    )

    # Push ingest: the token in the upload URL is the credential. The console's own
    # PASSKEY is remembered on first use and must match afterwards, so a leaked URL
    # used from a different device is rejected (and visible in the ingest log).
    push_token = models.CharField(max_length=64, unique=True, default=_new_push_token, editable=False)
    push_passkey = models.CharField(max_length=64, blank=True,
                                    help_text='Learned from the first accepted upload. Clear it to re-learn.')
    ambient_api_enabled = models.BooleanField(
        'poll Ambient API', default=True,
        help_text='Fetch archive records from ambientweather.net (needs AMBIENT_* keys in .env).',
    )

    forecast_enabled = models.BooleanField(
        'show forecast', default=True,
        help_text='Show a daily forecast from Open-Meteo on the dashboard. Sends the station\'s approximate '
                  'location (rounded to about 1 km) to open-meteo.com.')
    RAIN_GAUGE_CHOICES = [
        ('auto', 'Automatic'),
        ('tipping', 'Tipping-bucket gauge'),
        ('piezo', 'Piezo (haptic) sensor'),
    ]
    rain_gauge = models.CharField(
        max_length=8, choices=RAIN_GAUGE_CHOICES, default='auto',
        help_text='Which rain sensor to record when the console reports two (e.g. an Ecowitt WS90 plus a '
                  'tipping-bucket gauge). Automatic prefers the tipping bucket. Applies to new uploads.',
    )
    sensors = models.JSONField(
        default=dict, blank=True, editable=False,
        help_text='Extra sensors seen in uploads, keyed by upload name: {"name": "...", "public": bool}.',
    )

    # Earliest observation timestamp written since the rollups last ran; the
    # rollup job recomputes from here and clears it.
    rollup_dirty_from = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.mac_address = normalize_mac(self.mac_address) or self.mac_address
        if not self.slug:
            base = slugify(self.name)[:90] or 'station'
            slug, n = base, 2
            while Station.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f'{base}-{n}', n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def tzinfo(self):
        return ZoneInfo(self.timezone)

    def rotate_push_token(self):
        self.push_token = _new_push_token()
        self.push_passkey = ''
        self.save(update_fields=['push_token', 'push_passkey'])

    def passkey_candidates(self):
        """Values a console might legitimately send as PASSKEY for this station:
        the MAC in common spellings and the MD5 of it (Ecowitt protocol)."""
        mac = self.mac_address.upper()
        plain = mac.replace(':', '')
        spellings = {mac, plain, mac.replace(':', '-')}
        hashes = {hashlib.md5(s.encode()).hexdigest().upper() for s in spellings}
        return {s.upper() for s in spellings} | hashes


class Observation(models.Model):
    """One archive interval for one station. All values SI; NULL = not reported.

    `timestamp` is the END of the interval (the Ambient archive and WeeWX
    convention): the row covers (timestamp − interval_s, timestamp]. A row stamped
    00:00 local belongs to the previous day, so rollups must truncate
    `timestamp − 1 s`, never `timestamp` itself.

    Raw rows are one archive interval (`interval_s`, usually 300 s). Downsampled
    rows cover 900 s and carry the *_min/*_max columns; on raw rows those stay NULL
    and readers fall back to the plain column (Coalesce(x_min, x)).
    """
    SOURCE_AMBIENT_PUSH = 'ambient_push'
    SOURCE_ECOWITT_PUSH = 'ecowitt_push'
    SOURCE_API = 'api'
    SOURCE_BACKFILL = 'backfill'
    SOURCE_DOWNSAMPLED = 'downsampled'
    SOURCE_CHOICES = [
        (SOURCE_AMBIENT_PUSH, 'Ambient push'),
        (SOURCE_ECOWITT_PUSH, 'Ecowitt push'),
        (SOURCE_API, 'Ambient API poll'),
        (SOURCE_BACKFILL, 'Ambient API backfill'),
        (SOURCE_DOWNSAMPLED, 'Downsampled'),
    ]

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='observations')
    timestamp = models.DateTimeField(help_text='UTC end of the interval.')
    interval_s = models.PositiveIntegerField(default=300)
    sample_count = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)

    temp_c = models.FloatField(null=True, blank=True)
    humidity = models.FloatField(null=True, blank=True)
    dewpoint_c = models.FloatField(null=True, blank=True)
    pressure_rel_hpa = models.FloatField(null=True, blank=True)
    pressure_abs_hpa = models.FloatField(null=True, blank=True)
    wind_speed_ms = models.FloatField(null=True, blank=True)
    wind_gust_ms = models.FloatField(null=True, blank=True)
    wind_dir_deg = models.FloatField(null=True, blank=True)
    rain_rate_mmh = models.FloatField(null=True, blank=True)
    rain_event_mm = models.FloatField(null=True, blank=True)
    rain_daily_mm = models.FloatField(null=True, blank=True)
    # The console's running total (totalrainin) as reported, and the rain that fell
    # during this row, derived from counter deltas by weather.ingest.rain. Sum
    # rain_mm; never sum the daily/event totals.
    rain_counter_mm = models.FloatField(null=True, blank=True)
    rain_mm = models.FloatField(null=True, blank=True)
    solar_wm2 = models.FloatField(null=True, blank=True)
    uv_index = models.FloatField(null=True, blank=True)
    temp_in_c = models.FloatField(null=True, blank=True)
    humidity_in = models.FloatField(null=True, blank=True)

    # Downsampled rows only.
    temp_min_c = models.FloatField(null=True, blank=True)
    temp_max_c = models.FloatField(null=True, blank=True)
    humidity_min = models.FloatField(null=True, blank=True)
    humidity_max = models.FloatField(null=True, blank=True)
    pressure_min_hpa = models.FloatField(null=True, blank=True)
    pressure_max_hpa = models.FloatField(null=True, blank=True)
    solar_max_wm2 = models.FloatField(null=True, blank=True)
    uv_max = models.FloatField(null=True, blank=True)

    extra = models.JSONField(default=dict, blank=True,
                             help_text='Additional sensors and console fields, keyed by their upload names.')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['station', 'timestamp'], name='uniq_observation_station_timestamp'),
        ]
        ordering = ['station', 'timestamp']

    def __str__(self):
        return f'{self.station} @ {self.timestamp:%Y-%m-%d %H:%M}Z'


# Core columns a parsed reading can fill (everything except bookkeeping/min/max).
OBSERVATION_FIELDS = (
    'temp_c', 'humidity', 'dewpoint_c', 'pressure_rel_hpa', 'pressure_abs_hpa',
    'wind_speed_ms', 'wind_gust_ms', 'wind_dir_deg', 'rain_rate_mmh', 'rain_event_mm',
    'rain_daily_mm', 'rain_counter_mm', 'solar_wm2', 'uv_index', 'temp_in_c', 'humidity_in',
)


class LatestReading(models.Model):
    """The newest reading per station, overwritten on every push/poll. Live tiles
    read this so they never touch the observation table."""
    station = models.OneToOneField(Station, on_delete=models.CASCADE, primary_key=True, related_name='latest')
    timestamp = models.DateTimeField(help_text='When the console took the reading (UTC).')
    received_at = models.DateTimeField(auto_now=True)
    source = models.CharField(max_length=16, choices=Observation.SOURCE_CHOICES)
    data = models.JSONField(default=dict, help_text='SI values keyed like Observation columns.')
    extra = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f'{self.station} latest @ {self.timestamp:%Y-%m-%d %H:%M:%S}Z'


class IngestCapture(models.Model):
    """Raw push requests, recorded only when INGEST_CAPTURE is on (and always for
    rejected requests that named a valid station). For tuning parsers against
    real console traffic; purged after INGEST_CAPTURE_RETENTION_DAYS."""
    station = models.ForeignKey(Station, on_delete=models.CASCADE, null=True, blank=True, related_name='captures')
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    protocol = models.CharField(max_length=16)
    method = models.CharField(max_length=8)
    remote_addr = models.CharField(max_length=45, blank=True)
    path = models.CharField(max_length=500)
    payload = models.TextField(help_text='Query string or form body.')
    accepted = models.BooleanField()
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.protocol} {"ok" if self.accepted else "REJECTED"} @ {self.received_at:%Y-%m-%d %H:%M:%S}'


class SiteSettings(models.Model):
    """Single-row site configuration, edited by staff at /site/settings/."""
    site_title = models.CharField(max_length=60, default='WS4Free',
                                  help_text='Shown in the header and browser tab, e.g. "Mesa Ridge Weather".')
    tagline = models.CharField(max_length=140, blank=True, help_text='A short line shown beside the site title in the header, e.g. "Backyard weather since 2021".')
    about = models.TextField(blank=True, help_text='A few sentences about the station and site, shown to visitors.')
    default_station = models.ForeignKey('Station', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                        help_text='Shown at the site\'s home page when there is more than one station.')

    class Meta:
        verbose_name = 'site settings'
        verbose_name_plural = 'site settings'

    def __str__(self):
        return self.site_title

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class RollupFields(models.Model):
    """Summary statistics shared by hourly and daily rollups (SI units).

    Averages are weighted by each observation's interval, so raw 5-minute and
    downsampled 15-minute rows mix correctly; min/max read the downsampled rows'
    *_min/*_max columns where present. `coverage` is the fraction of the period
    with data — a day with 3 hours of readings must not set a record low.
    """
    sample_count = models.PositiveIntegerField(default=0)
    covered_s = models.PositiveIntegerField(default=0, help_text='Seconds of the period with observations.')
    coverage = models.FloatField(default=0, help_text='covered_s / period length (0–1).')
    # Per-sensor coverage: the console can keep reporting while one sensor is
    # offline (a day with indoor readings but no outdoor temperature). Records and
    # totals must check the coverage of the quantity they use.
    temp_coverage = models.FloatField(default=0, help_text='Fraction of the period with outdoor temperature.')
    rain_coverage = models.FloatField(default=0, help_text='Fraction of the period with a rain amount.')

    temp_avg_c = models.FloatField(null=True, blank=True)
    temp_min_c = models.FloatField(null=True, blank=True)
    temp_max_c = models.FloatField(null=True, blank=True)
    humidity_avg = models.FloatField(null=True, blank=True)
    humidity_min = models.FloatField(null=True, blank=True)
    humidity_max = models.FloatField(null=True, blank=True)
    dewpoint_avg_c = models.FloatField(null=True, blank=True)
    dewpoint_min_c = models.FloatField(null=True, blank=True)
    dewpoint_max_c = models.FloatField(null=True, blank=True)
    pressure_avg_hpa = models.FloatField(null=True, blank=True)
    pressure_min_hpa = models.FloatField(null=True, blank=True)
    pressure_max_hpa = models.FloatField(null=True, blank=True)
    wind_speed_avg_ms = models.FloatField(null=True, blank=True)
    wind_speed_max_ms = models.FloatField(null=True, blank=True)
    wind_gust_max_ms = models.FloatField(null=True, blank=True)
    wind_dir_avg_deg = models.FloatField(null=True, blank=True, help_text='Speed-weighted vector mean; NULL when calm.')
    rain_mm = models.FloatField(null=True, blank=True)
    rain_rate_max_mmh = models.FloatField(null=True, blank=True)
    solar_avg_wm2 = models.FloatField(null=True, blank=True)
    solar_max_wm2 = models.FloatField(null=True, blank=True)
    uv_avg = models.FloatField(null=True, blank=True)
    uv_max = models.FloatField(null=True, blank=True)
    temp_in_avg_c = models.FloatField(null=True, blank=True)
    temp_in_min_c = models.FloatField(null=True, blank=True)
    temp_in_max_c = models.FloatField(null=True, blank=True)
    humidity_in_avg = models.FloatField(null=True, blank=True)
    # NWS apparent temperature, from each reading's temperature, humidity and wind:
    # lowest wind chill (≤ 50 °F, wind ≥ 3 mph) and highest heat index (≥ 80 °F).
    windchill_min_c = models.FloatField(null=True, blank=True)
    heatindex_max_c = models.FloatField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True,
                             help_text='Extra sensors: {upload key: [mean, min, max]} in SI units.')

    class Meta:
        abstract = True


class HourlyRollup(RollupFields):
    """One UTC hour: observations with timestamp in (period_start, period_start + 1 h]."""
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='hourly')
    period_start = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['station', 'period_start'], name='uniq_hourly_station_period')]
        ordering = ['station', 'period_start']

    def __str__(self):
        return f'{self.station} {self.period_start:%Y-%m-%d %H}:00Z'


class DailyRollup(RollupFields):
    """One local calendar day in the station's time zone."""
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='daily')
    date = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['station', 'date'], name='uniq_daily_station_date')]
        ordering = ['station', 'date']

    def __str__(self):
        return f'{self.station} {self.date}'


# Measurement groups an exclusion can cover, and the Observation columns each one
# nulls. Dew point depends on both temperature and humidity, so either removes it;
# rain removes the counters too, or the rain pass would rebuild rain_mm from them.
EXCLUSION_GROUPS = {
    'temp': ('Outdoor temperature', ('temp_c', 'temp_min_c', 'temp_max_c', 'dewpoint_c')),
    'humidity': ('Outdoor humidity', ('humidity', 'humidity_min', 'humidity_max', 'dewpoint_c')),
    'wind': ('Wind', ('wind_speed_ms', 'wind_gust_ms', 'wind_dir_deg')),
    'rain': ('Rain', ('rain_mm', 'rain_counter_mm', 'rain_daily_mm', 'rain_event_mm', 'rain_rate_mmh')),
    'pressure': ('Pressure', ('pressure_rel_hpa', 'pressure_abs_hpa', 'pressure_min_hpa', 'pressure_max_hpa')),
    'solar': ('Sun and UV', ('solar_wm2', 'solar_max_wm2', 'uv_index', 'uv_max')),
    'indoor': ('Indoor', ('temp_in_c', 'humidity_in')),
}


class DataExclusion(models.Model):
    """A period whose readings for some measurements are known to be wrong (a failing
    sensor, a gauge knocked over). The values are moved to ExcludedValue — not
    deleted — so everything downstream treats them as missing, and removing the
    exclusion restores them exactly. See weather.quality."""
    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='exclusions')
    start = models.DateTimeField()
    end = models.DateTimeField(null=True, blank=True, help_text='Blank = still ongoing; new readings are excluded too.')
    groups = models.JSONField(help_text='Keys of EXCLUSION_GROUPS.')
    reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    readings = models.PositiveIntegerField(default=0, help_text='Readings affected when last applied.')
    STATUS_CHOICES = [('pending', 'Applying'), ('applied', 'Applied'), ('removing', 'Removing'), ('failed', 'Failed')]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')

    class Meta:
        ordering = ['-start']

    def __str__(self):
        return f'{self.station}: {", ".join(self.groups)} from {self.start:%Y-%m-%d}'

    def fields(self):
        cols = []
        for key in self.groups:
            for col in EXCLUSION_GROUPS.get(key, ('', ()))[1]:
                if col not in cols:
                    cols.append(col)
        return cols

    def group_labels(self):
        return [EXCLUSION_GROUPS[g][0] for g in self.groups if g in EXCLUSION_GROUPS]


class ExcludedValue(models.Model):
    """The original value of one Observation column, held while an exclusion covers it."""
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name='excluded_values')
    field = models.CharField(max_length=32)
    value = models.FloatField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['observation', 'field'], name='uniq_excluded_observation_field')]


class TempCalibration(models.Model):
    """A correction for a temperature sensor that reads wrong in a known way (a
    poorly shielded sensor runs warm in sunshine and cold on clear nights).

        corrected = raw − Δ,   Δ = night + day·[sun up] + solar · S/1000

    with coefficients in °C (solar in °C per 1000 W/m²) for each calendar month.
    Reference mode fits them against a nearby reference station; manual mode
    uses one set for every month. Like exclusions, applying keeps the original
    values (CalibratedValue) and is fully reversible. See weather.calibration.
    """
    MODE_CHOICES = [('reference', 'Fitted to a reference station'), ('manual', 'Manual')]
    STATUS_CHOICES = [
        ('fitting', 'Fitting'), ('fitted', 'Ready to review'), ('pending', 'Applying'),
        ('applied', 'Applied'), ('removing', 'Removing'), ('failed', 'Failed'),
    ]

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='calibrations')
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True, blank=True, help_text='Blank = still ongoing; new readings are corrected too.')
    coefficients = models.JSONField(default=dict, blank=True,
                                    help_text='{"1": {"night": °C, "day": °C, "solar": °C per 1000 W/m²}, … "12": …}')
    # Reference mode
    reference_station = models.CharField(max_length=10, blank=True, help_text='Airport/ASOS identifier, e.g. DEN.')
    baseline_start = models.DateField(null=True, blank=True)
    baseline_end = models.DateField(null=True, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True, help_text='Progress or the reason a fit failed.')

    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    readings = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start']

    def __str__(self):
        return f'{self.station}: {self.get_mode_display()} temperature calibration from {self.start:%Y-%m-%d}'


class CalibratedValue(models.Model):
    """The original value of a temperature column while a calibration corrects it."""
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name='calibrated_values')
    calibration = models.ForeignKey(TempCalibration, on_delete=models.CASCADE, related_name='originals')
    field = models.CharField(max_length=32)
    value = models.FloatField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['observation', 'field'], name='uniq_calibrated_observation_field')]


class StationForecast(models.Model):
    """The latest daily forecast for a station (weather.forecast), refreshed at most
    hourly when the dashboard is viewed. Kept so a slow or unreachable forecast
    service never holds up the page: the last good forecast is shown instead."""
    station = models.OneToOneField(Station, on_delete=models.CASCADE, primary_key=True, related_name='forecast')
    fetched_at = models.DateTimeField(null=True, blank=True, help_text='When `days` was last fetched successfully.')
    attempted_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    days = models.JSONField(default=list, blank=True,
                            help_text='[{date, code, high_c, low_c, rain_pct, rain_mm}, …], SI units.')

    def __str__(self):
        return f'Forecast for {self.station}'


class StationEvent(models.Model):
    """A dated note in a station's log: it was moved, a sensor was replaced, a battery
    changed… Shown as markers on the charts so later readers know why the data changed."""
    KIND_CHOICES = [
        ('moved', 'Moved'), ('sensor', 'Sensor replaced'), ('maintenance', 'Maintenance'),
        ('battery', 'Battery changed'), ('outage', 'Outage'), ('other', 'Other'),
    ]

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='events')
    occurred_at = models.DateTimeField(help_text='UTC. Local midnight when no time was given.')
    has_time = models.BooleanField(default=False)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default='other')
    title = models.CharField(max_length=120)
    notes = models.TextField(blank=True)
    is_public = models.BooleanField('public', default=True,
                                    help_text='Visitors see public entries on the charts; private ones only you see.')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']

    def __str__(self):
        return f'{self.station}: {self.title} ({self.occurred_at:%Y-%m-%d})'
