from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.html import format_html

from .forms import CarAdminForm, CarImageForm
from .images import build_derivatives
from .tasks import process_pending
from .management.commands.ensure_inventory_group import (
    GROUP_NAME as INVENTORY_GROUP_NAME,
)
from . import qa
from .booking import cancel_by_staff, confirm_booking, ensure_slots
from .booking_models import (
    ACTIVE_STATUSES,
    BookingStatus,
    CustomerProfile,
    TestDriveBooking,
    TestDriveSchedule,
    TestDriveSlot,
)
from .models import Car, CarImage, StaffAccount
from .qa_models import CarQuestion


class CarImageInline(admin.TabularInline):
    model = CarImage
    form = CarImageForm
    extra = 3
    fields = ("image", "preview", "is_primary", "order")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return "—"
        # Prefer the smallest generated copy; the original can be several megabytes and
        # the admin renders it at 70px tall.
        urls = obj.derivative_urls
        src = urls[min(urls)] if urls else obj.image.url
        note = "" if obj.derivatives_ready else " (optimising…)"
        return format_html(
            '<img src="{}" style="height:70px;border-radius:4px" />{}', src, note
        )


@admin.register(Car)
class CarAdmin(admin.ModelAdmin):
    form = CarAdminForm
    inlines = [CarImageInline]

    class Media:
        # Sends photos and video straight to S3, sidestepping the ~4.5 MB ceiling that
        # uploading through Lambda imposes.
        js = ("cars/direct-upload.js",)
        css = {"all": ("cars/direct-upload.css",)}
    list_display = (
        "manufacture_year",
        "brand",
        "model_name",
        "grade",
        "display_price",
        "status",
    )
    list_display_links = ("brand", "model_name")
    list_filter = ("status", "fuel_type", "brand")
    search_fields = ("brand", "model_name", "model_code", "chassis_number")
    list_editable = ("status",)
    actions = ["rebuild_derivatives"]
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            "Vehicle",
            {
                "fields": (
                    "brand",
                    "model_name",
                    "grade",
                    "model_code",
                    "chassis_number",
                    "manufacture_year",
                )
            },
        ),
        ("Specification", {"fields": ("fuel_type", "seat_capacity", "color")}),
        ("Listing", {"fields": ("price_jpy", "status")}),
        (
            "Video",
            {
                "fields": ("video",),
                "description": (
                    "Optional MP4 walkaround. Nothing downloads until a visitor presses "
                    "play, so it costs nothing on page load. MP4 only - iPhone "
                    "'High Efficiency' clips are HEVC/.mov and will not play in Chrome "
                    "or Firefox."
                ),
            },
        ),
        (
            "Description",
            {
                "fields": ("description_en", "description_ja"),
                "description": (
                    "Either language may be left blank — the site falls back to "
                    "whichever one is filled in."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Price", ordering="price_jpy")
    def display_price(self, obj):
        if obj.price_jpy is None:
            return "Call for price"
        return f"¥{obj.price_jpy:,}"

    def save_model(self, request, obj, form, change):
        if "video" in form.changed_data or getattr(obj, "video_direct_name", None):
            obj.video_uploaded_at = timezone.now() if obj.video else None
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Catch up on anything an earlier save ran out of time for. Free to do here:
        # the database is already awake for this request, so it costs no extra Aurora
        # time, unlike a scheduled sweep would.
        caught_up = process_pending()
        if caught_up:
            messages.info(
                request, f"Also optimised {caught_up} photo(s) left from an earlier save."
            )

    @admin.action(description="Rebuild optimised copies")
    def rebuild_derivatives(self, request, queryset):
        """Manual repair for photos that never finished processing."""
        rebuilt = failed = 0
        for car in queryset:
            for image in car.images.all():
                try:
                    build_derivatives(image)
                    rebuilt += 1
                except Exception:
                    failed += 1
        if rebuilt:
            self.message_user(request, f"Rebuilt {rebuilt} photo(s).")
        if failed:
            self.message_user(
                request, f"{failed} photo(s) failed.", level=messages.ERROR
            )


@admin.register(CarImage)
class CarImageAdmin(admin.ModelAdmin):
    list_display = ("__str__", "car", "is_primary", "order")
    list_filter = ("is_primary",)


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


@admin.register(TestDriveSlot)
class TestDriveSlotAdmin(admin.ModelAdmin):
    """Actual dates. This is where "that Friday is closed" gets done."""

    list_display = ("starts_at", "ends_at", "capacity", "seats_taken", "is_open")
    list_filter = ("is_open", "starts_at")
    list_editable = ("is_open",)
    date_hierarchy = "starts_at"
    ordering = ("starts_at",)
    readonly_fields = ("schedule",)

    @admin.display(description="Booked")
    def seats_taken(self, obj):
        return f"{obj.booked_count} / {obj.capacity}"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("bookings")

    @admin.action(description="Close selected (customers keep existing bookings)")
    def close_slots(self, request, queryset):
        updated = queryset.update(is_open=False)
        self.message_user(
            request,
            f"Closed {updated} slot(s). Anyone already booked still has their "
            "appointment - cancel those individually if the day is off.",
        )

    @admin.action(description="Re-open selected")
    def open_slots(self, request, queryset):
        self.message_user(request, f"Re-opened {queryset.update(is_open=True)} slot(s).")

    actions = ["close_slots", "open_slots"]


@admin.register(TestDriveBooking)
class TestDriveBookingAdmin(admin.ModelAdmin):
    """Soonest first.

    Until notification emails exist, this page is the only way anyone finds out a
    customer is coming - so it leads with when, who, and how to reach them.
    """

    list_display = (
        "slot", "customer_name", "customer_phone", "customer_email",
        "car_label", "status",
    )
    list_filter = ("status", "slot__starts_at")
    ordering = ("status", "slot__starts_at")
    search_fields = (
        "customer__first_name", "customer__last_name", "customer__email",
        "customer__customer_profile__phone", "car_label",
    )
    # No list_editable. Ticking a status in the changelist writes through a formset
    # built by get_changelist_form(), which never uses ModelAdmin.form - and a bulk save
    # that fires several irreversible confirmation emails from one click is the wrong
    # affordance anyway. The actions below do the same job and say what they will do.
    date_hierarchy = "slot__starts_at"
    readonly_fields = (
        "slot", "customer", "car", "car_label", "created_at", "updated_at",
        "confirmed_at", "cancelled_at",
    )

    def save_model(self, request, obj, form, change):
        """Route a status change through the domain function that emails the customer.

        Saving the model directly would move the status and tell nobody - which is
        exactly what the old list_editable did. save_model is called by both the change
        form and the changelist formset, so putting it here closes the hole whichever
        way someone reaches it.
        """
        moved_to = form.cleaned_data.get("status") if change else None
        if moved_to and moved_to != form.initial.get("status"):
            # Hand the domain function the row as it still stands, not `obj` - the form
            # has already written the new status onto the instance, and confirm_booking
            # returns early when it is handed a booking that is confirmed already. That
            # early return is right; passing it a half-applied object is not.
            stored = TestDriveBooking.objects.get(pk=obj.pk)
            if moved_to == BookingStatus.CONFIRMED:
                confirm_booking(stored)
                return
            if moved_to == BookingStatus.CANCELLED:
                cancel_by_staff(stored)
                return
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("slot", "customer", "customer__customer_profile", "car")
        )

    @admin.display(description="Customer")
    def customer_name(self, obj):
        return obj.customer.get_full_name() or obj.customer.username

    @admin.display(description="Phone")
    def customer_phone(self, obj):
        profile = getattr(obj.customer, "customer_profile", None)
        return profile.phone if profile else "—"

    @admin.display(description="Email")
    def customer_email(self, obj):
        return obj.customer.email

    @admin.action(description="Confirm selected (emails the customer)")
    def confirm_bookings(self, request, queryset):
        """The only thing that tells a customer their appointment is on."""
        confirmed = 0
        for booking in queryset.exclude(status=BookingStatus.CONFIRMED):
            if booking.status in ACTIVE_STATUSES:
                confirm_booking(booking)
                confirmed += 1
        if confirmed:
            self.message_user(
                request, f"Confirmed {confirmed} booking(s). The customer has been emailed."
            )
        else:
            self.message_user(
                request,
                "Nothing to confirm - those are already confirmed or no longer active.",
                level=messages.WARNING,
            )

    @admin.action(description="Cancel selected (tells the customer)")
    def cancel_bookings(self, request, queryset):
        cancelled = 0
        for booking in queryset.filter(status__in=ACTIVE_STATUSES):
            cancel_by_staff(booking)
            cancelled += 1
        self.message_user(
            request, f"Cancelled {cancelled} booking(s) and let the customer know."
        )

    actions = ["confirm_bookings", "cancel_bookings"]


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    """Read-only. Customers manage their own details; staff only need to look."""

    list_display = ("__str__", "phone", "created_at")
    search_fields = ("user__first_name", "user__last_name", "user__email", "phone")
    readonly_fields = ("user", "phone", "created_at")

    def has_add_permission(self, request):
        return False


class CarQuestionAdminForm(forms.ModelForm):
    """Friendly refusal on the change form.

    Courtesy, not the guard. A ModelForm's clean() does not run on the changelist -
    get_changelist_form() never passes ModelAdmin.form - so the thing that actually holds
    is the CheckConstraint on the model. This exists so a staff member gets a sentence
    instead of an IntegrityError page.
    """

    class Meta:
        model = CarQuestion
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_published") and not (cleaned.get("answer") or "").strip():
            raise forms.ValidationError(
                {"answer": "Write an answer before publishing. A published question "
                           "with no answer is worse than no question at all."}
            )
        return cleaned


@admin.register(CarQuestion)
class CarQuestionAdmin(admin.ModelAdmin):
    """The queue of things buyers want to know.

    Answering emails the customer immediately. Publishing is a separate decision, so a
    private reply stays private until someone decides the answer is worth showing.

    No list_editable, for the reason spelled out on TestDriveBookingAdmin: the changelist
    formset skips this form entirely, so a tick-box there would go around the publish
    guard. The two actions do the job and say what they will do.
    """

    form = CarQuestionAdminForm
    list_display = ("car", "excerpt", "state_label", "language", "created_at")
    list_filter = ("is_published", "language", "car__brand")
    search_fields = ("question", "answer", "car__brand", "car__model_name",
                     "customer__email")
    ordering = ("-created_at",)
    actions = ["publish_selected", "unpublish_selected"]

    fieldsets = (
        (None, {"fields": ("car", "customer", "language", "created_at")}),
        ("The question", {
            "fields": ("question",),
            "description": "Editable. People type their phone number and their name into "
                           "free text, and this goes on a public page - tidy it before "
                           "you publish.",
        }),
        ("Your answer", {
            "fields": ("answer", "answered_by", "answered_at"),
            "description": "Saving an answer emails the customer straight away. Editing "
                           "one you have already sent does not email them again.",
        }),
        ("Public page", {
            "fields": ("is_published",),
            "description": "Puts the question and your answer on the car's page and in "
                           "search results. The customer's name is never shown. The page "
                           "is cached, so allow up to five minutes for it to appear.",
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("car", "customer", "answered_by")

    def get_readonly_fields(self, request, obj=None):
        base = ("customer", "created_at", "answered_at", "answered_by")
        return base + ("car",) if obj else base

    @admin.display(description="Question")
    def excerpt(self, obj):
        text = obj.question.strip().replace("\n", " ")
        return text[:70] + ("…" if len(text) > 70 else "")

    @admin.display(description="State", ordering="answered_at")
    def state_label(self, obj):
        return obj.state

    def save_model(self, request, obj, form, change):
        """Answers go through the domain function, which decides about the email."""
        if change and "answer" in form.changed_data:
            super().save_model(request, obj, form, change)
            qa.record_answer(obj, answer=obj.answer, staff=request.user)
            return
        super().save_model(request, obj, form, change)

    @admin.action(description="Publish selected on the car's page")
    def publish_selected(self, request, queryset):
        published, skipped = 0, []
        for question in queryset:
            try:
                qa.publish(question)
                published += 1
            except qa.QuestionError:
                skipped.append(str(question.pk))
        if published:
            self.message_user(
                request,
                f"Published {published}. The car page is cached, so allow up to five "
                f"minutes for it to show.",
            )
        if skipped:
            self.message_user(
                request,
                f"Left {len(skipped)} unpublished - they have no answer yet.",
                level=messages.ERROR,
            )

    @admin.action(description="Remove selected from the car's page")
    def unpublish_selected(self, request, queryset):
        for question in queryset:
            qa.unpublish(question)
        self.message_user(request, f"Removed {queryset.count()} from the public page.")
