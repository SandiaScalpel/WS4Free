"""Who may do what with a station.

Roles, weakest first: a visitor (None) sees public stations; a viewer also sees a
private station, its indoor readings, private sensors and private log entries; an
advanced viewer also sees batteries and neighbours and every Manage tab, read-only and
without the upload paths; a manager changes the station's settings and data; the owner
also decides who else has access, whether the station is public, and who owns it; site
administrators (staff) can do everything to every station. Viewers, advanced viewers
and managers are StationAccess rows; the owner is Station.owner."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Station, StationAccess, StationEvent

VIEWER, ADVANCED, MANAGER, OWNER, STAFF = 'viewer', 'advanced', 'manager', 'owner', 'staff'
_RANK = {None: 0, VIEWER: 1, ADVANCED: 2, MANAGER: 3, OWNER: 4, STAFF: 5}


def role(user, station):
    """The user's role for the station, or None. Cached on the user for the request."""
    if user is None or not user.is_authenticated or not user.is_active:
        return None
    if user.is_staff:
        return STAFF
    if station.owner_id == user.pk:
        return OWNER
    cache = getattr(user, '_ws4f_access', None)
    if cache is None:
        cache = user._ws4f_access = dict(StationAccess.objects.filter(user=user).values_list('station_id', 'role'))
    return cache.get(station.pk)


def at_least(user, station, wanted):
    return _RANK[role(user, station)] >= _RANK[wanted]


def can_view(user, station):
    return station.is_public or at_least(user, station, VIEWER)


def can_see_private(user, station):
    """Indoor readings, private sensors and private log entries."""
    return at_least(user, station, VIEWER)


def can_see_management(user, station):
    """Batteries, the neighbours line, and the Manage tabs read-only (no upload paths)."""
    return at_least(user, station, ADVANCED)


def can_manage(user, station):
    """Change settings, data quality, calibration, log, neighbours, gateways; see upload paths."""
    return at_least(user, station, MANAGER)


def can_administer(user, station):
    """Who else has access, public or private, and ownership."""
    return at_least(user, station, OWNER)


def visible_stations(user):
    """Public stations plus, for a signed-in user, those they own or were given access to
    (staff see all)."""
    if user.is_authenticated and user.is_active and user.is_staff:
        return Station.objects.all()
    if user.is_authenticated and user.is_active:
        return Station.objects.filter(Q(is_public=True) | Q(owner=user) | Q(access__user=user)).distinct()
    return Station.objects.filter(is_public=True)


def display_name(user):
    full = user.get_full_name()
    return f'{full} ({user.get_username()})' if full else user.get_username()


@transaction.atomic
def transfer(station, new_owner, by, keep_previous=True):
    """Make `new_owner` the station's owner. The previous owner stays on as a manager
    when `keep_previous`; the new owner's own access row, if any, goes (owners need
    none). Recorded as a private entry in the station log."""
    previous = station.owner
    if previous.pk == new_owner.pk:
        return
    StationAccess.objects.filter(station=station, user=new_owner).delete()
    Station.objects.filter(pk=station.pk).update(owner=new_owner)
    station.owner = new_owner
    if keep_previous:
        StationAccess.objects.update_or_create(station=station, user=previous,
                                               defaults={'role': StationAccess.MANAGER, 'added_by': by})
    notes = f'From {display_name(previous)} to {display_name(new_owner)}, by {display_name(by)}.'
    if keep_previous:
        notes += f' {display_name(previous)} stays on as a manager.'
    StationEvent.objects.create(station=station, occurred_at=timezone.now(), has_time=True, kind='other',
                                title=f'Ownership transferred to {display_name(new_owner)}', notes=notes,
                                is_public=False, created_by=by)


def station_allowance(user):
    """(may add a station now, stations they may still add) — None remaining = no limit."""
    if not (user.is_authenticated and user.is_active):
        return False, 0
    if user.is_staff:
        return True, None
    profile = getattr(user, 'profile', None)
    if profile is None or not profile.may_add_stations:
        return False, 0
    left = max(profile.station_limit - Station.objects.filter(owner=user).count(), 0)
    return left > 0, left


def can_add_station(user):
    return station_allowance(user)[0]


def own_home_station(user):
    """For a signed-in station owner who isn't an administrator, their own (oldest) station:
    their home page, instead of the site's default station."""
    if not (user.is_authenticated and user.is_active) or user.is_staff:
        return None
    return Station.objects.filter(owner=user).order_by('created_at', 'pk').first()
