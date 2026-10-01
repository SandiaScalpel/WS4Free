import datetime as dt

from django.contrib.auth import get_user_model

from weather.ingest.parsers import Reading
from weather.models import Observation, Station

from .fixtures import MAC

T0 = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)


def make_station(**kwargs):
    owner = kwargs.pop('owner', None) or get_user_model().objects.get_or_create(username='owner')[0]
    defaults = dict(owner=owner, name='Test Station', mac_address=MAC, timezone='America/Denver')
    return Station.objects.create(**{**defaults, **kwargs})


def reading(minutes=0, seconds=0, **values):
    return Reading(T0 + dt.timedelta(minutes=minutes, seconds=seconds), values)


def obs(station, minutes, **values):
    return Observation.objects.create(station=station, timestamp=T0 + dt.timedelta(minutes=minutes),
                                      source=Observation.SOURCE_API, **values)
