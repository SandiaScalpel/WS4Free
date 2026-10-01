from zoneinfo import available_timezones

from django import forms

import datetime as dt

from django.utils import timezone

from django.db.models import Q

from .models import EXCLUSION_GROUPS, DataExclusion, SiteSettings, Station, TempCalibration
from .units import FEET_PER_METER, default_prefs


class StationSettingsForm(forms.ModelForm):
    """What a station owner can change from the app. Identity fields that history
    depends on (slug, MAC, archive interval) are deliberately absent."""
    elevation = forms.FloatField(required=False, min_value=-500, max_value=30000)
    anemometer_height = forms.FloatField(min_value=0.5, max_value=100)

    class Meta:
        model = Station
        fields = ['name', 'place', 'is_public', 'timezone', 'latitude', 'longitude', 'ambient_api_enabled', 'rain_gauge']
        labels = {
            'is_public': 'Public station',
            'ambient_api_enabled': 'Fill gaps from ambientweather.net',
            'rain_gauge': 'Rain sensor',
        }
        help_texts = {
            'name': 'Shown on the dashboard and in page titles.',
            'place': 'Shown under the name, e.g. "Boulder, Colorado". Optional.',
            'is_public': 'Anyone can view the dashboard and charts without signing in. '
                         'Settings, the upload URL and the upload log are never public.',
            'timezone': 'Defines the station\'s local day: daily totals, highs and lows, and when rain resets.',
            'ambient_api_enabled': 'Every 5 minutes, fetch any readings the console\'s uploads missed '
                                   '(needs the AMBIENT_* keys in .env).',
        }
        widgets = {
            'timezone': forms.TextInput(attrs={'list': 'tz-list', 'autocomplete': 'off', 'spellcheck': 'false'}),
            'latitude': forms.NumberInput(attrs={'step': 'any'}),
            'longitude': forms.NumberInput(attrs={'step': 'any'}),
        }

    def __init__(self, *args, prefs=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Elevation follows the viewer's length units: feet for anyone measuring rain in inches.
        self.imperial = (prefs or default_prefs()).rain == 'in'
        unit = 'ft' if self.imperial else 'm'
        self.fields['elevation'].label = f'Elevation ({unit})'
        self.fields['elevation'].help_text = 'Above sea level. Used for evapotranspiration and sea-level pressure.'
        if self.instance.elevation_m is not None:
            value = self.instance.elevation_m * FEET_PER_METER if self.imperial else self.instance.elevation_m
            self.initial['elevation'] = round(value)
        self.fields['anemometer_height'].label = f'Wind sensor height ({unit})'
        self.fields['anemometer_height'].help_text = ('Above the ground. Used to adjust wind to the 2 m standard for '
                                                      'evapotranspiration (a roof-mounted sensor reads windier).')
        h = self.instance.anemometer_height_m or 2.0
        self.initial['anemometer_height'] = round(h * FEET_PER_METER, 1) if self.imperial else round(h, 1)
        self.fields['rain_gauge'].required = False
        # One name + public switch per extra sensor the station has reported.
        from .sensors import KIND_ORDER, describe
        self.sensor_rows = []
        known = [(key, conf, describe(key)) for key, conf in (self.instance.sensors or {}).items() if describe(key)]
        known.sort(key=lambda k: (KIND_ORDER.index(k[2].kind), k[2].channel, k[0]))   # as on the dashboard
        for i, (key, conf, sensor) in enumerate(known):
            name_field, public_field = f'sensor_{i}_name', f'sensor_{i}_public'
            self.fields[name_field] = forms.CharField(max_length=40, required=False, label=f'Name for {key}',
                                                      widget=forms.TextInput(attrs={'placeholder': sensor.default_label}))
            self.fields[public_field] = forms.BooleanField(required=False, label='Public')
            self.initial[name_field] = conf.get('name', '')
            self.initial[public_field] = bool(conf.get('public'))
            self.sensor_rows.append({'key': key, 'sensor': sensor, 'name': self[name_field], 'public': self[public_field]})
        self.fields['latitude'].help_text = 'Decimal degrees, north positive.'
        self.fields['longitude'].help_text = 'Decimal degrees, east positive (the Americas are negative).'

    @staticmethod
    def timezone_choices():
        return sorted(available_timezones())

    def clean_rain_gauge(self):
        return self.cleaned_data.get('rain_gauge') or self.instance.rain_gauge or 'auto'

    def clean_latitude(self):
        value = self.cleaned_data.get('latitude')
        if value is not None and not -90 <= value <= 90:
            raise forms.ValidationError('Latitude must be between −90 and 90.')
        return value

    def clean_longitude(self):
        value = self.cleaned_data.get('longitude')
        if value is not None and not -180 <= value <= 180:
            raise forms.ValidationError('Longitude must be between −180 and 180.')
        return value

    def save(self, commit=True):
        station = super().save(commit=False)
        elevation = self.cleaned_data.get('elevation')
        if elevation is None:
            station.elevation_m = None
        else:
            station.elevation_m = elevation / FEET_PER_METER if self.imperial else elevation
        height = self.cleaned_data['anemometer_height']
        station.anemometer_height_m = height / FEET_PER_METER if self.imperial else height
        sensors = dict(station.sensors or {})
        for row in self.sensor_rows:
            sensors[row['key']] = {'name': self.cleaned_data[row['name'].name].strip(),
                                   'public': self.cleaned_data[row['public'].name]}
        station.sensors = sensors
        if commit:
            station.save()
        return station


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ['site_title', 'tagline', 'about', 'default_station']
        widgets = {'about': forms.Textarea(attrs={'rows': 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['default_station'].empty_label = 'First public station'


class ExclusionForm(forms.Form):
    """Times are entered in the station's local time zone."""
    start = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
                                input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d'])
    end = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
                              input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d'],
                              help_text='Leave blank if the problem is still going on: new readings are excluded too.')
    groups = forms.MultipleChoiceField(choices=[(k, v[0]) for k, v in EXCLUSION_GROUPS.items()],
                                       widget=forms.CheckboxSelectMultiple, label='Measurements to exclude')
    reason = forms.CharField(max_length=200, required=False,
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. humidity sensor failing, replaced Oct 2023'}))

    def __init__(self, *args, station, **kwargs):
        super().__init__(*args, **kwargs)
        self.station = station

    def full_clean(self):
        # Django makes naive form datetimes aware in the *current* time zone (the
        # site's, UTC); the owner typed station-local times.
        with timezone.override(self.station.tzinfo):
            super().full_clean()

    def _aware(self, value):
        if value is None:
            return None
        if timezone.is_naive(value):
            value = value.replace(tzinfo=self.station.tzinfo)
        return value.astimezone(dt.UTC)

    def clean(self):
        data = super().clean()
        start, end = self._aware(data.get('start')), self._aware(data.get('end'))
        if start and end and end <= start:
            self.add_error('end', 'The end must be after the start.')
        if start and start > timezone.now():
            self.add_error('start', 'The start can\'t be in the future.')
        data['start'], data['end'] = start, end
        return data

    def save(self, user):
        return DataExclusion.objects.create(
            station=self.station, start=self.cleaned_data['start'], end=self.cleaned_data['end'],
            groups=self.cleaned_data['groups'], reason=self.cleaned_data['reason'], created_by=user)


class CalibrationForm(forms.Form):
    """Temperature calibration. Times in the station's time zone; manual offsets in
    the viewer's temperature unit (stored in °C)."""
    mode = forms.ChoiceField(choices=TempCalibration.MODE_CHOICES, widget=forms.RadioSelect, initial='reference')
    start = forms.DateTimeField(label='Affected from', widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
                                input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d'],
                                help_text='When the sensor started reading wrong (e.g. when it was installed).')
    end = forms.DateTimeField(label='Affected until', required=False,
                              widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
                              input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d'],
                              help_text='Blank if it still reads wrong: new readings are corrected too.')
    reason = forms.CharField(max_length=200, required=False,
                             widget=forms.TextInput(attrs={'placeholder': 'e.g. replacement sensor reads warm in sun'}))
    reference_station = forms.CharField(max_length=10, required=False, label='Reference station',
                                        help_text='Nearby airport weather station identifier, e.g. DEN (or KDEN).')
    baseline_start = forms.DateField(required=False, label='Trusted from', widget=forms.DateInput(attrs={'type': 'date'}))
    baseline_end = forms.DateField(required=False, label='Trusted until', widget=forms.DateInput(attrs={'type': 'date'}),
                                   help_text='A period when this sensor read correctly, ideally a full year.')
    night = forms.FloatField(required=False, label='Night offset')
    day = forms.FloatField(required=False, label='Extra daytime offset')
    solar = forms.FloatField(required=False, label='Solar slope', help_text='Additional error per 1000 W/m² of sunshine.')

    def __init__(self, *args, station, prefs, **kwargs):
        super().__init__(*args, **kwargs)
        self.station, self.prefs = station, prefs
        unit = prefs.label('temp')
        self.fields['night'].help_text = f'How much too warm ({unit}) the sensor reads at night; negative if too cold.'
        self.fields['day'].help_text = f'Extra error ({unit}) whenever the sun is up.'
        self.fields['solar'].label = f'Solar slope ({unit} per 1000 W/m²)'

    def full_clean(self):
        with timezone.override(self.station.tzinfo):
            super().full_clean()

    def _aware(self, value):
        if value is None:
            return None
        if timezone.is_naive(value):
            value = value.replace(tzinfo=self.station.tzinfo)
        return value.astimezone(dt.UTC)

    def clean_reference_station(self):
        code = self.cleaned_data.get('reference_station', '').strip().upper()
        # The archive uses three-letter identifiers for US stations (DEN, not KDEN).
        if len(code) == 4 and code.startswith('K'):
            code = code[1:]
        return code

    def clean(self):
        data = super().clean()
        start, end = self._aware(data.get('start')), self._aware(data.get('end'))
        data['start'], data['end'] = start, end
        if start and end and end <= start:
            self.add_error('end', 'The end must be after the start.')
        if start and start > timezone.now():
            self.add_error('start', 'The start can\'t be in the future.')
        if start:
            clash = TempCalibration.objects.filter(station=self.station).exclude(status='failed').filter(
                Q(end__isnull=True) | Q(end__gt=start))
            if end:
                clash = clash.filter(start__lt=end)
            if clash.exists():
                self.add_error('start', 'This period overlaps an existing calibration; remove that one first.')
        if data.get('mode') == 'reference':
            if not data.get('reference_station'):
                self.add_error('reference_station', 'Enter a reference station.')
            bs, be = data.get('baseline_start'), data.get('baseline_end')
            if not bs or not be:
                self.add_error('baseline_end', 'Enter the period when the sensor was trusted.')
            elif be <= bs:
                self.add_error('baseline_end', 'The end must be after the start.')
            elif (be - bs).days < 30:
                self.add_error('baseline_end', 'Use at least a month; a full year captures every season.')
            elif start and dt.datetime.combine(be, dt.time(), tzinfo=self.station.tzinfo) > start:
                self.add_error('baseline_end', 'The trusted period must end before the affected period starts.')
        else:
            if all(data.get(f) in (None, 0) for f in ('night', 'day', 'solar')):
                self.add_error('night', 'Enter at least one non-zero offset.')
        return data

    def save(self, user):
        d = self.cleaned_data
        common = dict(station=self.station, mode=d['mode'], start=d['start'], end=d['end'], reason=d['reason'],
                      created_by=user)
        if d['mode'] == 'reference':
            return TempCalibration.objects.create(**common, reference_station=d['reference_station'],
                                                  baseline_start=d['baseline_start'], baseline_end=d['baseline_end'],
                                                  status='fitting', message='Fetching reference data…')
        to_c = (lambda v: v * 5 / 9) if self.prefs.temp == 'F' else (lambda v: v)
        coef = {'night': to_c(d['night'] or 0.0), 'day': to_c(d['day'] or 0.0), 'solar': to_c(d['solar'] or 0.0)}
        return TempCalibration.objects.create(**common, status='pending',
                                              coefficients={str(m): dict(coef) for m in range(1, 13)})
