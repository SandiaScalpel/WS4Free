from WS4Free import __version__

from .models import SiteSettings
from .units import prefs_for_request


def site(request):
    from .access import can_add_station, visible_stations
    user = getattr(request, 'user', None)
    # The header's Stations menu lists them; a handful per site, so one small query.
    stations = list(visible_stations(user).only('name', 'slug').order_by('name')) if user is not None else []
    return {'site': SiteSettings.get(), 'units': prefs_for_request(request),
            'nav_stations': stations,
            'visible_station_count': len(stations),
            # Header menu: administrators have it under Manage stations; users they allowed get their own entry.
            'can_add_own_station': (user is not None and user.is_authenticated and not user.is_staff
                                    and can_add_station(user)),
            'ws4free_version': __version__.replace('.dev0', '-dev')}
