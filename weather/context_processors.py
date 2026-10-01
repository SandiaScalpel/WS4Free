from WS4Free import __version__

from .models import SiteSettings
from .units import prefs_for_request


def site(request):
    from .views import visible_stations
    user = getattr(request, 'user', None)
    return {'site': SiteSettings.get(), 'units': prefs_for_request(request),
            'visible_station_count': visible_stations(user).count() if user is not None else 0,
            'ws4free_version': __version__.replace('.dev0', '-dev')}
