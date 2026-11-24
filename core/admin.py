from django.contrib import admin

from .models import APIKey, Delivery, Endpoint, Event, Tenant


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspect-only. State changes belong in core/transitions.py """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(APIKey)
class APIKeyAdmin(ReadOnlyAdmin):
    list_display = ("name", "prefix", "tenant", "is_active", "created_at")
    list_filter = ("is_active",)
    list_select_related = ("tenant",)


@admin.register(Endpoint)
class EndpointAdmin(ReadOnlyAdmin):
    list_display = ("url", "tenant", "is_active", "deleted_at", "disabled_at")
    list_filter = ("is_active",)
    list_select_related = ("tenant",)


@admin.register(Event)
class EventAdmin(ReadOnlyAdmin):
    list_display = ("event_type", "id", "tenant", "created_at")
    list_select_related = ("tenant",)


@admin.register(Delivery)
class DeliveryAdmin(ReadOnlyAdmin):
    list_display = ("id", "state", "dead_reason", "next_attempt_at", "tenant")
    list_filter = ("state", "dead_reason")
    list_select_related = ("tenant",)