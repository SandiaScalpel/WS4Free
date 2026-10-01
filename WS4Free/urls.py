from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path
from two_factor.urls import urlpatterns as tf_urls

urlpatterns = [
    path('admin/', admin.site.urls),
    path('account/logout/', LogoutView.as_view(), name='logout'),
    path('', include(tf_urls)),
    path('webauthn/', include('django_otp_webauthn.urls', namespace='otp_webauthn')),
    path('', include('accounts.urls', namespace='accounts')),
    path('', include('weather.urls', namespace='weather')),
]
