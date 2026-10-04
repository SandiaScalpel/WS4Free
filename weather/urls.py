from django.urls import path, re_path

from . import views
from .ingest import views as ingest_views

app_name = 'weather'

urlpatterns = [
    path('', views.home, name='home'),
    path('units/', views.set_units, name='set-units'),
    path('stations/', views.station_list, name='stations'),
    path('stations/new/', views.station_create, name='station-create'),
    path('compare/', views.compare_page, name='compare'),
    path('compare/data/', views.compare_json, name='compare-data'),
    path('charts/', views.charts_home, name='charts'),
    path('site/settings/', views.site_settings, name='site-settings'),
    path('help/', views.help_page, name='help'),
    path('help/<slug:slug>/', views.help_page, name='help-page'),
    path('stations/<slug:slug>/', views.station_dashboard, name='station'),
    path('stations/<slug:slug>/live/', views.station_live, name='station-live'),
    path('stations/<slug:slug>/forecast/', views.station_forecast, name='station-forecast'),
    path('stations/<slug:slug>/forecast/<str:date>/', views.station_forecast_day, name='station-forecast-day'),
    path('stations/<slug:slug>/charts/', views.station_charts, name='station-charts'),
    path('stations/<slug:slug>/charts/data/', views.station_chart_data, name='station-chart-data'),
    path('stations/<slug:slug>/almanac/', views.station_almanac, name='station-almanac'),
    path('stations/<slug:slug>/reports/', views.station_reports, name='station-reports'),
    path('stations/<slug:slug>/growing/', views.station_growing, name='station-growing'),
    path('stations/<slug:slug>/settings/', views.station_settings, name='station-settings'),
    path('stations/<slug:slug>/setup/', views.station_setup, name='station-setup'),
    path('stations/<slug:slug>/forget-passkey/', views.station_forget_passkey, name='station-forget-passkey'),
    path('stations/<slug:slug>/quality/', views.station_quality, name='station-quality'),
    path('stations/<slug:slug>/quality/<int:pk>/remove/', views.station_quality_remove, name='station-quality-remove'),
    path('stations/<slug:slug>/calibration/<int:pk>/apply/', views.station_calibration_apply, name='station-calibration-apply'),
    path('stations/<slug:slug>/calibration/<int:pk>/remove/', views.station_calibration_remove, name='station-calibration-remove'),
    path('stations/<slug:slug>/log/', views.station_log, name='station-log'),
    path('stations/<slug:slug>/log/<int:pk>/', views.station_log, name='station-log-edit'),
    path('stations/<slug:slug>/log/<int:pk>/delete/', views.station_log_delete, name='station-log-delete'),
    path('stations/<slug:slug>/neighbours/', views.station_neighbours, name='station-neighbours'),
    path('stations/<slug:slug>/neighbours/<int:pk>/include/', views.station_neighbour_include, name='station-neighbour-include'),
    path('stations/<slug:slug>/neighbours/<int:pk>/delete/', views.station_neighbour_delete, name='station-neighbour-delete'),
    path('stations/<slug:slug>/gateways/add/', views.station_gateway_add, name='station-gateway-add'),
    path('stations/<slug:slug>/gateways/<int:pk>/rotate/', views.station_gateway_rotate, name='station-gateway-rotate'),
    path('stations/<slug:slug>/gateways/<int:pk>/delete/', views.station_gateway_delete, name='station-gateway-delete'),
    path('stations/<slug:slug>/rotate-token/', views.station_rotate_token, name='station-rotate-token'),
    # Consoles append their parameters to the configured path, sometimes without a
    # '?', so everything after the token is captured and parsed as parameters.
    re_path(r'^ingest/ambient/(?P<token>[A-Za-z0-9_-]+)(?P<rest>.*)$', ingest_views.ambient_push, name='ingest-ambient'),
    re_path(r'^ingest/ecowitt/(?P<token>[A-Za-z0-9_-]+)(?P<rest>.*)$', ingest_views.ecowitt_push, name='ingest-ecowitt'),
]
