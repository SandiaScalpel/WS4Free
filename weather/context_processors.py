from .models import SiteSettings
from .units import prefs_for_request


def site(request):
    return {'site': SiteSettings.get(), 'units': prefs_for_request(request)}
