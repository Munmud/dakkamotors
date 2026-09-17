from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.html import format_html

from .management.commands.ensure_inventory_group import (
    GROUP_NAME as INVENTORY_GROUP_NAME,
)
from .booking import ensure_slots
from .booking_models import CustomerProfile, TestDriveSchedule
from .models import StaffAccount


class StaffAccountForm(UserChangeForm):
    """Guards that belong to the data rather than to a particular view.

    Built on UserChangeForm rather than a plain ModelForm so `password` stays the
    read-only hash field with its "change password" link. A plain ModelForm turns it
    into a required text input, and every save fails with "This field is required".
    """

    class Meta(UserChangeForm.Meta):
        model = StaffAccount
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        editor = getattr(self, "editing_user", None)

        # One careless tick would otherwise log you out of your own account with no way
        # back in short of asking the owner.
        if editor is not None and self.instance.pk == editor.pk:
            if "is_active" in cleaned and not cleaned["is_active"]:
                self.add_error("is_active", "You cannot deactivate your own account.")
            if "is_staff" in cleaned and not cleaned["is_staff"]:
                self.add_error("is_staff", "You cannot remove your own staff access.")
        return cleaned


@admin.register(StaffAccount)
class StaffAccountAdmin(DjangoUserAdmin):
    """Staff administration that a non-superuser can be trusted with.

    Django's stock UserAdmin plus `auth.change_user` is a complete privilege
    escalation: the holder can open the owner's account and reset its password, tick
    "superuser" on themselves, or grant themselves any permission that exists. None of
    that is a bug - it is what the permission means. So members of Inventory Managers
    get this sandbox instead, and never an `auth` permission at all.

    A superuser gets Django's behaviour untouched.
    """

    form = StaffAccountForm
    list_display = ("username", "get_full_name", "email", "is_active", "is_staff")
    list_filter = ("is_active", "is_staff", "groups")
    ordering = ("username",)

    #: Groups a non-superuser is allowed to hand out. Anything more powerful added
    #: later is invisible here, so it cannot be joined by someone sandboxed.
    ASSIGNABLE_GROUPS = (INVENTORY_GROUP_NAME,)

    SANDBOXED_FIELDSETS = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        (
            "Access",
            {
                "fields": ("is_active", "is_staff", "groups"),
                "description": (
                    "Untick Active to stop someone logging in. Accounts are never "
                    "deleted here, so their edit history stays intact."
                ),
            },
        ),
    )

    # --- who is looking -------------------------------------------------------------

    @staticmethod
    def _unrestricted(request):
        return request.user.is_superuser

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        # Customers hold accounts in the same table but are not staff, and have no
        # business appearing in staff administration - listing them here would let a
        # manager read and reset the passwords of the public.
        queryset = queryset.filter(is_staff=True)
        if self._unrestricted(request):
            return queryset
        # The owner's account is not listed, searchable, or selectable.
        return queryset.filter(is_superuser=False)

    def _forbidden_target(self, request, obj):
        return obj is not None and obj.is_superuser and not self._unrestricted(request)

    # Checked as well as filtering the queryset, so a guessed primary key in the URL is
    # refused rather than merely absent from the list.
    def has_view_permission(self, request, obj=None):
        if self._forbidden_target(request, obj):
            return False
        return super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if self._forbidden_target(request, obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # Deactivate, never delete: reversible, and it keeps the admin history readable.
        if not self._unrestricted(request):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self._unrestricted(request):
            actions.pop("delete_selected", None)
        return actions

    # --- what they can edit ---------------------------------------------------------

    def get_fieldsets(self, request, obj=None):
        if obj is None or self._unrestricted(request):
            return super().get_fieldsets(request, obj)
        # No is_superuser (self-promotion) and no user_permissions (granting anything).
        return self.SANDBOXED_FIELDSETS

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Let the form recognise "this is me" for the self-lockout guard.
        form.editing_user = request.user
        return form

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "groups" and not self._unrestricted(request):
            kwargs["queryset"] = Group.objects.filter(name__in=self.ASSIGNABLE_GROUPS)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    # --- what actually gets written -------------------------------------------------

    def save_model(self, request, obj, form, change):
        if not self._unrestricted(request):
            # Belt and braces: a crafted POST cannot set these even if the field were
            # somehow rendered.
            obj.is_superuser = False
            if not change:
                # A colleague who cannot log in is a confusing thing to hand someone.
                obj.is_staff = True
                obj.is_active = True
        super().save_model(request, obj, form, change)
        if not self._unrestricted(request):
            obj.user_permissions.clear()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if self._unrestricted(request):
            return
        obj = form.instance
        # New accounts join the role automatically; there is only one they could be
        # given, and an account with no group sees an empty admin.
        if not change:
            obj.groups.add(Group.objects.get(name=INVENTORY_GROUP_NAME))
        # Re-assert the allowlist after the form has written the M2M.
        for group in obj.groups.exclude(name__in=self.ASSIGNABLE_GROUPS):
            obj.groups.remove(group)


@admin.register(TestDriveSchedule)
class TestDriveScheduleAdmin(admin.ModelAdmin):
    """The recurring rules. Slots are generated from these on demand."""

    list_display = ("__str__", "capacity", "is_active", "starts_on", "ends_on", "note")
    list_filter = ("is_active", "weekday")
    list_editable = ("is_active",)
    fieldsets = (
        (
            "When",
            {
                "fields": ("weekday", "start_time", "end_time"),
                "description": "Times are Japan local time. A rule repeats every week.",
            },
        ),
        (
            "How many",
            {
                "fields": ("capacity",),
                "description": "How many customers can take this appointment at once - "
                "set 2 if two cars or two staff are free.",
            },
        ),
        (
            "Limits",
            {
                "fields": ("is_active", "starts_on", "ends_on", "note"),
                "description": "Untick Active to stop generating new slots. Slots "
                "already created keep their bookings; close them individually under "
                "Test drive slots.",
            },
        ),
    )

    @admin.action(description="Generate slots for the next 4 weeks")
    def generate(self, request, queryset):
        created = ensure_slots()
        self.message_user(request, f"Created {created} new slot(s).")

    actions = ["generate"]


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    """Read-only. Customers manage their own details; staff only need to look."""

    list_display = ("__str__", "phone", "created_at")
    search_fields = ("user__first_name", "user__last_name", "user__email", "phone")
    readonly_fields = ("user", "phone", "created_at")

    def has_add_permission(self, request):
        return False


# The Q&A admin used to live here. Questions moved to DynamoDB, and `ModelAdmin` is built
# on `QuerySet` and `ModelForm`, so there is no adapter -- the queue, the answer form and
# the publish/unpublish actions are now server-rendered staff pages under
# `cars/staff/`, reachable at /api/staff/questions/.
#
# The decisions that page inherits, so they are not lost with the class that held them:
#   * No bulk tick-box for publishing. The old changelist skipped ModelAdmin.form
#     entirely, so a tick there went round the publish guard; explicit buttons say what
#     they will do.
#   * Answering emails the customer immediately; publishing is a separate decision, so a
#     private reply stays private until someone decides the answer is worth showing.
#   * The question text stays editable before publishing: people type their phone number
#     and their name into free text, and it ends up on a public page.


# TestDriveSlotAdmin and TestDriveBookingAdmin used to live here. Slots and bookings
# moved to DynamoDB, and ModelAdmin is built on QuerySet, so both are now server-rendered
# staff pages under `cars/staff/` at /api/staff/slots/ and /api/staff/bookings/.
#
# What those pages inherit, so the reasoning is not lost with the classes:
#   * Confirm and cancel route through booking.confirm_booking / cancel_by_staff, never
#     through a direct status write. The old save_model had to re-fetch the stored row
#     to stop a half-applied status reaching the domain function; explicit buttons
#     remove the possibility instead of working around it.
#   * No list_editable and no tick-boxes. A bulk save firing several irreversible
#     confirmation emails from one click is the wrong affordance.
#   * Closing a slot stops new bookings and never cancels the ones already taken.
#   * date_hierarchy became an explicit from/to pair, which is what staff used the
#     drill-down for and what maps onto a range query.


# CarAdmin and CarImageInline used to live here. Cars and their photos moved to
# DynamoDB, so both are now server-rendered staff pages at /api/staff/cars/.
#
# What carried over, and what deliberately did not:
#   * Every fieldset grouping, help text and description string, because they were
#     written for the person typing and storage is no reason to re-learn the page.
#   * direct-upload.js and its CSS, essentially untouched: it derives the hidden field
#     from `input.name` with `.replace(/image$/, "image_key")`, and plain
#     `formset_factory` still names fields `form-0-image`.
#   * The standalone CarImage changelist is dropped. Photos are only ever reached
#     through their car.
#   * `process_pending()` on save is dropped. It existed because the database was
#     already awake for that request and a scheduled sweep would have kept Aurora
#     alive; neither is true any more.
