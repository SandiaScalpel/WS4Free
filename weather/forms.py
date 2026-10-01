from zoneinfo import available_timezones

from django import forms

import datetime as dt

from django.utils import timezone

from .models import EXCLUSION_GROUPS, DataExclusion, SiteSettings, Station
from .units import FEET_PER_METER, default_prefs


class StationSettingsForm(forms.ModelForm):
    """What a station owner can change from the app. Identity fields that history
    depends on (slug, MAC, archive interval) are deliberately absent."""
    elevation = forms.FloatField(required=False, min_value=-500, max_value=30000)
    anemometer_height = forms.FloatField(min_value=0.5, max_value=100)

    class Meta:
        model = Station
        fields = ['name', 'place', 'is_public', 'timezone', 'latitude', 'longitude', 'ambient_api_enabled']
        labels = {
            'is_public': 'Public station',
            'ambient_api_enabled': 'Fill gaps from ambientweather.net',
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
        self.fields['latitude'].help_text = 'Decimal degrees, north positive.'
        self.fields['longitude'].help_text = 'Decimal degrees, east positive (the Americas are negative).'

    @staticmethod
    def timezone_choices():
        return sorted(available_timezones())

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
