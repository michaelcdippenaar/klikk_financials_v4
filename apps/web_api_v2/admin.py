from django.contrib import admin

from .models import UserEntityMembership, ViewerPreference


@admin.register(UserEntityMembership)
class UserEntityMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'entity', 'role', 'active', 'updated_at')
    list_filter = ('active', 'role')
    search_fields = ('user__username', 'user__email', 'entity__tenant_id', 'entity__tenant_name')
    autocomplete_fields = ('user', 'entity')


@admin.register(ViewerPreference)
class ViewerPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'default_entity', 'default_financial_year', 'updated_at')
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user', 'default_entity')
