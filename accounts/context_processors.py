from django.conf import settings


def environment_name(request):
    return {'ENVIRONMENT_NAME': settings.ENVIRONMENT_NAME}


def user_profile(request):
    if not request.user.is_authenticated:
        return {'user_profile': None}
    from .models import UserProfile
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return {'user_profile': profile}
