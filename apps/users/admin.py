from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.users.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("-created_date",)
    list_display = ("email", "phone_number", "user_type", "is_active", "is_staff")
    list_filter = ("user_type", "is_active", "is_staff")
    search_fields = ("email", "phone_number", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "phone_number", "password")}),
        ("Profile", {"fields": ("full_name", "user_type")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "phone_number", "user_type", "password1", "password2"),
            },
        ),
    )
