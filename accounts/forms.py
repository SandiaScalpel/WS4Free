from django import forms
from django.contrib.auth import get_user_model

from .models import UserProfile


class UserForm(forms.ModelForm):
    """A user as site administrators add or edit them (Users page). No password: new
    users choose their own from a welcome link."""

    class Meta:
        model = get_user_model()
        fields = ['username', 'first_name', 'last_name', 'email', 'is_staff']
        labels = {'is_staff': 'Site administrator', 'email': 'Email (optional)'}
        help_texts = {
            'username': 'What they sign in with. Letters, digits and @ . + - _ only.',
            'email': 'For your reference; WS4Free doesn\'t send email.',
            'is_staff': 'Sees and manages every station, the site settings and the users.',
        }

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if editing:
            del self.fields['username']
        self.fields['first_name'].required = False
        self.fields['last_name'].required = False

    def clean_username(self):
        username = self.cleaned_data['username']
        model = get_user_model()
        if model.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Someone already has this username.')
        return username


class StationAllowanceForm(forms.ModelForm):
    """Whether a non-administrator may add stations of their own, and how many they may own."""

    class Meta:
        model = UserProfile
        fields = ['may_add_stations', 'station_limit']
        help_texts = {
            'may_add_stations': 'They see Add a station and become the owner of what they add. Their stations don\'t '
                                'use the site\'s Ambient Weather or Weather Underground keys (no gap-filling, no neighbours).',
            'station_limit': 'Stations they may own in all, counting any you transfer to them.',
        }
        widgets = {'station_limit': forms.NumberInput(attrs={'min': 1, 'max': 100})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['station_limit'].required = False     # hidden while the box is unticked

    def clean_station_limit(self):
        limit = self.cleaned_data['station_limit']
        if limit is None:
            return self.instance.station_limit
        if not 1 <= limit <= 100:
            raise forms.ValidationError('Between 1 and 100.')
        return limit
