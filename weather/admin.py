from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from .models import DailyRollup, HourlyRollup, IngestCapture, LatestReading, Observation, Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'mac_address', 'timezone', 'is_public', 'ambient_api_enabled', 'latest_reading', 'setup_link')
    list_filter = ('is_public', 'ambient_api_enabled')
    search_fields = ('name', 'mac_address', 'slug')
    readonly_fields = ('rollup_dirty_from', 'created_at', 'setup_link')
    actions = ['rotate_push_tokens']
    fieldsets = (
        (None, {'fields': ('owner', 'name', 'slug', 'mac_address', 'is_public')}),
        ('Location', {'fields': ('timezone', 'latitude', 'longitude', 'elevation_m')}),
        ('Ingest', {'fields': ('setup_link', 'archive_interval_s', 'push_passkey', 'ambient_api_enabled')}),
        ('Status', {'fields': ('rollup_dirty_from', 'created_at')}),
    )

    @admin.display(description='Latest reading')
    def latest_reading(self, obj):
        latest = getattr(obj, 'latest', None)
        return f'{latest.timestamp:%Y-%m-%d %H:%M}Z ({latest.get_source_display()})' if latest else '—'

    @admin.display(description='Console setup')
    def setup_link(self, obj):
        if not obj.pk:
            return 'Save the station first.'
        return format_html('<a href="{}">Upload URLs and ingest log</a>', reverse('weather:station-setup', args=[obj.slug]))

    @admin.action(description='Rotate push token (console must be reconfigured)')
    def rotate_push_tokens(self, request, queryset):
        for station in queryset:
            station.rotate_push_token()
        messages.success(request, f'Rotated {queryset.count()} token(s).')


@admin.register(Observation)
class ObservationAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'station', 'source', 'temp_c', 'humidity', 'pressure_rel_hpa',
                    'wind_speed_ms', 'wind_gust_ms', 'rain_mm', 'sample_count')
    list_filter = ('station', 'source')
    list_select_related = ('station',)
    show_full_result_count = False   # COUNT(*) over millions of rows on every page is slow
    ordering = ('-timestamp',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LatestReading)
class LatestReadingAdmin(admin.ModelAdmin):
    list_display = ('station', 'timestamp', 'received_at', 'source')
    readonly_fields = ('station', 'timestamp', 'received_at', 'source', 'data', 'extra')

    def has_add_permission(self, request):
        return False


@admin.register(IngestCapture)
class IngestCaptureAdmin(admin.ModelAdmin):
    list_display = ('received_at', 'station', 'protocol', 'method', 'accepted', 'note', 'remote_addr')
    list_filter = ('accepted', 'protocol', 'station')
    readonly_fields = [f.name for f in IngestCapture._meta.fields]

    def has_add_permission(self, request):
        return False


class _RollupAdmin(admin.ModelAdmin):
    list_filter = ('station',)
    list_select_related = ('station',)
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(HourlyRollup)
class HourlyRollupAdmin(_RollupAdmin):
    list_display = ('period_start', 'station', 'temp_avg_c', 'temp_min_c', 'temp_max_c', 'rain_mm', 'wind_gust_max_ms', 'coverage')
    ordering = ('-period_start',)


@admin.register(DailyRollup)
class DailyRollupAdmin(_RollupAdmin):
    list_display = ('date', 'station', 'temp_min_c', 'temp_max_c', 'rain_mm', 'wind_gust_max_ms', 'coverage')
    ordering = ('-date',)
    date_hierarchy = 'date'
