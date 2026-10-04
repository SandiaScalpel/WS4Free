from WS4Free import __version__

from .models import SiteSettings
from .units import prefs_for_request


def site(request):
    from .views import visible_stations
    user = getattr(request, 'user', None)
    # The header's Stations menu lists them; a handful per site, so one small query.
    stations = list(visible_stations(user).only('name', 'slug').order_by('name')) if user is not None else []
    return {'site': SiteSettings.get(), 'units': prefs_for_request(request),
            'nav_stations': stations,
            'visible_station_count': len(stations),
            'ws4free_version': __version__.replace('.dev0', '-dev')}
