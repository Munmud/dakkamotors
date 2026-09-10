"""Test drive scheduling and bookings.

Staff describe availability as recurring weekly rules. Those become concrete
`TestDriveSlot` rows, which is what customers book against and what staff edit when a
particular evening is off.

Slots are real rows rather than something computed on the fly for two reasons: a booking
needs a stable thing to point at, and "close this Friday" is far easier to reason about
when staff can see the actual date and untick it.
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


class TestDriveSchedule(models.Model):
    """A recurring weekly availability rule.

    "Every Friday 18:30-19:00, 2 people" is one row. It describes intent; the slots it
    produces are what actually get booked.
    """

    weekday = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField(help_text="Local time in Japan, e.g. 18:30.")
    end_time = models.TimeField()
    capacity = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many customers can take this appointment at once - two cars or "
        "two staff free means 2.",
    )
    is_active = models.BooleanField(default=True)
    starts_on = models.DateField(
        null=True, blank=True, help_text="Optional. Leave blank to start immediately."
    )
    ends_on = models.DateField(
        null=True, blank=True, help_text="Optional. Leave blank to run indefinitely."
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["weekday", "start_time", "end_time"],
                name="unique_test_drive_rule",
            )
        ]

    def __str__(self):
        return (
            f"{self.get_weekday_display()} "
            f"{self.start_time:%H:%M}-{self.end_time:%H:%M} "
            f"({self.capacity} at a time)"
        )

    def covers(self, day):
        if not self.is_active or day.weekday() != self.weekday:
            return False
        if self.starts_on and day < self.starts_on:
            return False
        if self.ends_on and day > self.ends_on:
            return False
        return True


class TestDriveSlot(models.Model):
    """One concrete appointment window, generated from a schedule."""

    schedule = models.ForeignKey(
        TestDriveSchedule,
        related_name="slots",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The rule this came from. Cleared if the rule is deleted, so slots "
        "people already booked survive.",
    )
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    # Copied from the rule rather than read through it: editing "every Friday" must not
    # retroactively shrink an evening someone has already booked, and staff can adjust
    # one awkward date without touching the rule.
    capacity = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    is_open = models.BooleanField(
        default=True,
        help_text="Untick to close this one appointment. Regenerating slots leaves "
        "your choice alone.",
    )

    class Meta:
        ordering = ["starts_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "starts_at"], name="unique_slot_per_rule_occurrence"
            )
        ]

    def __str__(self):
        local = timezone.localtime(self.starts_at)
        return f"{local:%a %d %b %Y %H:%M}"

    @property
    def booked_count(self):
        return self.bookings.filter(status__in=ACTIVE_STATUSES).count()

    @property
    def seats_left(self):
        return max(self.capacity - self.booked_count, 0)

    @property
    def is_bookable(self):
        """Cheap display check. The API re-checks properly under a row lock."""
        return self.is_open and self.seats_left > 0 and self.starts_at > timezone.now()


class BookingStatus(models.TextChoices):
    # A request until staff accept it. The seat is held meanwhile - otherwise two
    # customers could both be pending for one place and one would have to be turned
    # away afterwards, which is worse than briefly showing the slot as full.
    PENDING = "pending", "Awaiting confirmation"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"
    COMPLETED = "completed", "Completed"
    NO_SHOW = "no_show", "Did not attend"


#: Statuses that occupy a seat and count towards a customer's limit.
ACTIVE_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


class TestDriveBooking(models.Model):
    slot = models.ForeignKey(
        TestDriveSlot, related_name="bookings", on_delete=models.CASCADE
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="test_drive_bookings",
        on_delete=models.CASCADE,
    )
    car = models.ForeignKey(
        "cars.Car",
        related_name="test_drive_bookings",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Deleting a sold car already wipes its photos from S3. The booking has to outlive
    # it, and staff still need to know what the appointment was about.
    car_label = models.CharField(max_length=160, blank=True)
    status = models.CharField(
        max_length=12, choices=BookingStatus.choices, default=BookingStatus.PENDING
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["slot__starts_at"]
        constraints = [
            # One live booking per customer per slot. Cancelled rows are kept as
            # history, so the constraint has to be conditional.
            models.UniqueConstraint(
                fields=["slot", "customer"],
                condition=models.Q(status__in=["pending", "confirmed"]),
                name="one_live_booking_per_customer_per_slot",
            )
        ]

    def __str__(self):
        return f"{self.customer} - {self.slot} ({self.get_status_display()})"

    @property
    def is_active(self):
        """Holds a seat: either awaiting confirmation or confirmed."""
        return self.status in ACTIVE_STATUSES

    def save(self, *args, **kwargs):
        if self.car and not self.car_label:
            self.car_label = self.car.seo_title_plain
        super().save(*args, **kwargs)


class CustomerProfile(models.Model):
    """The phone number, which Django's user model has nowhere to put.

    A profile rather than a custom user model: swapping AUTH_USER_MODEL on a database
    that already has accounts is a risky migration for no benefit here.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="customer_profile",
        on_delete=models.CASCADE,
    )
    phone = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.phone})"


class PendingRegistration(models.Model):
    """A sign-up that has not proved its email address yet.

    No `User` row exists until the link in the email is clicked, which is why nothing
    downstream needs to ask whether an account is verified - an unverified person simply
    has no account. This table is where they wait.

    Two things are deliberately stored hashed. The password so plaintext never touches
    the database, and the token so that a database leak cannot hand someone a working
    activation link: the raw token exists only in the email.
    """

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32)
    password_hash = models.CharField(max_length=256)
    token_hash = models.CharField(max_length=64, db_index=True)
    # Where to send them once the account exists, so a half-finished booking survives
    # the trip through their inbox. Validated on the way in - see auth_views.safe_next.
    next_path = models.CharField(max_length=200, blank=True)
    language = models.CharField(max_length=5, default="en")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} (pending until {self.expires_at:%Y-%m-%d})"

    @property
    def has_expired(self):
        return timezone.now() >= self.expires_at
