from WS4Free import __version__

from .models import SiteSettings
from .units import prefs_for_request


def site(request):
    return {'site': SiteSettings.get(), 'units': prefs_for_request(request),
            'ws4free_version': __version__.replace('.dev0', '-dev')}
