"""Every item type in the table.

These mirror the Django models field-for-field **and keep their derived properties**.
That is the deliberate, load-bearing choice of this migration: `seo.py`, `mail.py`,
`email_theme.py` and most of `pages.py` read models only through
`seo_title_plain`, `get_absolute_url()`, `derivative_urls`, `covers()`, `seats_left`,
`state` and friends. Keep those names and signatures identical and roughly 1,400 lines
of downstream code need no edit at all.

Where behaviour necessarily differs it is called out in a comment rather than left for
someone to discover.
"""

import datetime as dt
import posixpath

from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.text import slugify
from pynamodb.attributes import (
    Attribute,
    BooleanAttribute,
    JSONAttribute,
    ListAttribute,
    NumberAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.constants import STRING

from ..choices import (
    ACTIVE_STATUSES,
    BookingStatus,
    CarStatus,
    FuelType,
    NotificationKind,
    QuestionLanguage,
    Weekday,
)
from .base import BaseItem


# --------------------------------------------------------------------------------------
# Attribute types DynamoDB has no native equivalent for
# --------------------------------------------------------------------------------------

class LocalTimeAttribute(Attribute):
    """A wall-clock time of day, stored as "HH:MM".

    Deliberately not a datetime: a recurring rule means "every Friday at 18:30 Japan
    time", which is a clock reading, not an instant. Storing it zero-padded keeps it
    sortable, which is what `schedule_gsi1sk` relies on.
    """

    attr_type = STRING

    def serialize(self, value):
        return value.strftime("%H:%M")

    def deserialize(self, value):
        hour, minute = value.split(":")
        return dt.time(int(hour), int(minute))


class LocalDateAttribute(Attribute):
    """A calendar date, stored ISO-8601. Sortable as a string by construction."""

    attr_type = STRING

    def serialize(self, value):
        return value.isoformat()

    def deserialize(self, value):
        return dt.date.fromisoformat(value)


def _display(choices, value):
    """Stand-in for Django's auto-generated `get_FOO_display()`."""
    if value is None:
        return ""
    try:
        return choices(value).label
    except ValueError:
        return str(value)


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------

class Car(BaseItem, discriminator="car"):
    car_id = UnicodeAttribute(null=True)

    brand = UnicodeAttribute(null=True)
    grade = UnicodeAttribute(null=True)
    model_name = UnicodeAttribute(null=True)
    model_code = UnicodeAttribute(null=True)
    chassis_number = UnicodeAttribute(null=True)
    manufacture_year = NumberAttribute(null=True)
    fuel_type = UnicodeAttribute(default=FuelType.PETROL)
    seat_capacity = NumberAttribute(default=5)
    color = UnicodeAttribute(null=True)

    # Left unset for "call for price" -- deliberately distinct from a price of zero.
    # PynamoDB omits a null attribute entirely rather than writing {"NULL": true}, so
    # `attribute_not_exists(price_jpy)` really does mean "no price", and the existing
    # test that a blank price serialises as null rather than 0 still holds.
    price_jpy = NumberAttribute(null=True)

    status = UnicodeAttribute(default=CarStatus.AVAILABLE)

    description_en = UnicodeAttribute(null=True)
    description_ja = UnicodeAttribute(null=True)

    video_name = UnicodeAttribute(null=True)
    video_uploaded_at = UTCDateTimeAttribute(null=True)

    slug = UnicodeAttribute(null=True)

    # Denormalised so the listing page is ONE query rather than one per card. Kept in
    # step by store.images._refresh_primary() on create / update / delete / reorder /
    # derivatives-ready -- all five paths, or a card goes stale.
    primary_image_ref = JSONAttribute(null=True)

    # Lowercased haystack for the staff substring search, written at save time. See
    # store/search.py for why the filtering happens in Python and not in a
    # FilterExpression.
    search_blob = UnicodeAttribute(null=True)

    created_at = UTCDateTimeAttribute(null=True)
    updated_at = UTCDateTimeAttribute(null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populated by store.cars.detail(); not a stored attribute.
        self.images = []
        self.questions = []

    def __str__(self):
        return f"{self.manufacture_year} {self.brand} {self.model_name}".strip()

    @property
    def seo_title_plain(self):
        """"2008 Daihatsu Tanto X" - the phrase a buyer would actually search for."""
        parts = [str(self.manufacture_year or ""), self.brand, self.model_name, self.grade]
        return " ".join(p for p in parts if p).strip()

    def get_absolute_url(self):
        return f"/cars/{self.slug}"

    def get_fuel_type_display(self):
        return _display(FuelType, self.fuel_type)

    def get_status_display(self):
        return _display(CarStatus, self.status)

    def build_slug_base(self):
        """The candidate stem only.

        The old `build_slug()` looped with `.exists()` until it found a free slug, which
        is check-then-write: two simultaneous saves of identically-named cars could both
        pass. Uniqueness now lives in a conditional write on the SLUG# guard item, and
        store.cars retries with a bumped suffix when that one condition fails, so the
        race is closed by the database instead of by the loop.
        """
        base = slugify(self.seo_title_plain) or slugify(self.chassis_number or "") or "car"
        return base[:110].rstrip("-")

    def build_search_blob(self):
        parts = [self.brand, self.grade, self.model_name, self.model_code,
                 self.chassis_number]
        return " ".join(p for p in parts if p).casefold()

    @property
    def primary_image(self):
        """The image to show on a listing card.

        Prefers whatever a detail query loaded; otherwise reconstructs a detached
        CarImage from the denormalised reference so a listing card needs no extra read.
        """
        if self.images:
            for image in self.images:
                if image.is_primary:
                    return image
            return self.images[0]

        ref = self.primary_image_ref
        if not ref:
            return None
        return CarImage(
            pk=self.pk,
            sk=ref.get("sk", ""),
            image_id=ref.get("id"),
            image_name=ref.get("name"),
            is_primary=True,
            derivatives_ready=bool(ref.get("ready")),
            derivative_widths=ref.get("widths") or [],
        )


class CarImage(BaseItem, discriminator="img"):
    image_id = UnicodeAttribute(null=True)
    car_id = UnicodeAttribute(null=True)

    image_name = UnicodeAttribute(null=True)
    is_primary = BooleanAttribute(default=False)
    order = NumberAttribute(default=0)

    derivatives_ready = BooleanAttribute(default=False)
    # A real list now rather than the comma-joined string the CharField forced. The
    # `available_widths` property below keeps the old shape for callers.
    derivative_widths = ListAttribute(of=NumberAttribute, null=True)

    created_at = UTCDateTimeAttribute(null=True)

    def __str__(self):
        return f"Image {self.image_id}"

    @property
    def available_widths(self):
        if not self.derivatives_ready or not self.derivative_widths:
            return []
        return [int(w) for w in self.derivative_widths]

    def derivative_name(self, width):
        """Storage name of one WebP copy, derived from the original's own name.

        Built from the full name rather than just its folder: photos uploaded through
        the ordinary path all land directly in ``cars/``, so a folder-based name would
        give every one of them the same ``cars/w800.webp`` and each upload would
        silently overwrite the last one's copies.
        """
        stem, _ = posixpath.splitext(self.image_name or "")
        return f"{stem}__w{width}.webp"

    @property
    def derivative_urls(self):
        """{width: url} for the copies that exist, or {} while none do."""
        return {w: default_storage.url(self.derivative_name(w))
                for w in self.available_widths}

    @property
    def url(self):
        return default_storage.url(self.image_name) if self.image_name else ""


# --------------------------------------------------------------------------------------
# Test drives
# --------------------------------------------------------------------------------------

class Schedule(BaseItem, discriminator="sched"):
    schedule_id = UnicodeAttribute(null=True)

    weekday = NumberAttribute(null=True)
    start_time = LocalTimeAttribute(null=True)
    end_time = LocalTimeAttribute(null=True)
    capacity = NumberAttribute(default=1)
    is_active = BooleanAttribute(default=True)
    starts_on = LocalDateAttribute(null=True)
    ends_on = LocalDateAttribute(null=True)
    note = UnicodeAttribute(null=True)

    def __str__(self):
        return (
            f"{self.get_weekday_display()} "
            f"{self.start_time:%H:%M}-{self.end_time:%H:%M} "
            f"({self.capacity} at a time)"
        )

    def get_weekday_display(self):
        return _display(Weekday, int(self.weekday) if self.weekday is not None else None)

    def covers(self, day):
        if not self.is_active or day.weekday() != int(self.weekday):
            return False
        if self.starts_on and day < self.starts_on:
            return False
        if self.ends_on and day > self.ends_on:
            return False
        return True


class Slot(BaseItem, discriminator="slot"):
    slot_id = UnicodeAttribute(null=True)
    schedule_id = UnicodeAttribute(null=True)

    starts_at = UTCDateTimeAttribute(null=True)
    ends_at = UTCDateTimeAttribute(null=True)
    # Copied from the rule rather than read through it: editing "every Friday" must not
    # retroactively shrink an evening someone has already booked.
    capacity = NumberAttribute(default=1)
    is_open = BooleanAttribute(default=True)

    # Was a COUNT(*) property on the Django model; now a maintained counter, because
    # the whole anti-double-booking design rests on comparing it to `capacity` inside a
    # ConditionExpression. The LIVE# seat items in this same partition remain the source
    # of truth -- see the reconcile_counters command.
    booked_count = NumberAttribute(default=0)

    def __str__(self):
        local = timezone.localtime(self.starts_at)
        return f"{local:%a %d %b %Y %H:%M}"

    @property
    def seats_left(self):
        return max(int(self.capacity) - int(self.booked_count), 0)

    @property
    def is_bookable(self):
        """Cheap display check. The write path re-checks atomically in a condition."""
        return self.is_open and self.seats_left > 0 and self.starts_at > timezone.now()


class Seat(BaseItem, discriminator="seat"):
    """The live-booking guard: `one_live_booking_per_customer_per_slot`.

    Created conditionally when a booking is taken and deleted unconditionally when it
    leaves ACTIVE_STATUSES, so its lifetime is exactly the booking's *active* lifetime
    -- which is precisely what the old partial unique constraint meant.

    It carries a snapshot of the customer so that `Query(SLOT#<id>, begins_with "LIVE#")`
    is also the staff roster for that appointment, phone number included, with no join.
    """

    booking_id = UnicodeAttribute(null=True)
    customer_sub = UnicodeAttribute(null=True)
    customer_name = UnicodeAttribute(null=True)
    customer_email = UnicodeAttribute(null=True)
    customer_phone = UnicodeAttribute(null=True)
    car_label = UnicodeAttribute(null=True)
    created_at = UTCDateTimeAttribute(null=True)


class Booking(BaseItem, discriminator="booking"):
    booking_id = UnicodeAttribute(null=True)
    customer_sub = UnicodeAttribute(null=True)
    slot_id = UnicodeAttribute(null=True)
    slot_starts_at = UTCDateTimeAttribute(null=True)
    slot_ends_at = UTCDateTimeAttribute(null=True)

    car_id = UnicodeAttribute(null=True)
    # Deleting a sold car wipes its photos from S3. The booking has to outlive it, and
    # staff still need to know what the appointment was about.
    car_label = UnicodeAttribute(null=True)
    # For the "about this car" link on the customer's own bookings list. Snapshotted for
    # the same reason as the label: the link may dangle, but the text must still read.
    car_slug = UnicodeAttribute(null=True)

    # Snapshot, for the staff queue. Refreshed across a customer's (at most three)
    # active bookings when they edit their profile.
    customer_name = UnicodeAttribute(null=True)
    customer_email = UnicodeAttribute(null=True)
    customer_phone = UnicodeAttribute(null=True)

    status = UnicodeAttribute(default=BookingStatus.PENDING)
    confirmed_at = UTCDateTimeAttribute(null=True)
    cancelled_at = UTCDateTimeAttribute(null=True)
    created_at = UTCDateTimeAttribute(null=True)
    updated_at = UTCDateTimeAttribute(null=True)

    def __str__(self):
        return f"{self.customer_email} - {self.slot_id} ({self.get_status_display()})"

    def get_status_display(self):
        return _display(BookingStatus, self.status)

    @property
    def is_active(self):
        """Holds a seat: either awaiting confirmation or confirmed."""
        return self.status in ACTIVE_STATUSES


# --------------------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------------------

class Customer(BaseItem, discriminator="cust"):
    """What survives of CustomerProfile once Cognito owns identity.

    Name, email and phone live on the Cognito user; this item exists for the counter,
    which has to be transactional with the booking write, and Cognito attributes cannot
    participate in a DynamoDB transaction.
    """

    customer_sub = UnicodeAttribute(null=True)
    email = UnicodeAttribute(null=True)
    active_bookings = NumberAttribute(default=0)
    created_at = UTCDateTimeAttribute(null=True)


# --------------------------------------------------------------------------------------
# Questions and notifications
# --------------------------------------------------------------------------------------

class CarQuestion(BaseItem, discriminator="q"):
    question_id = UnicodeAttribute(null=True)
    car_id = UnicodeAttribute(null=True)
    # Denormalised so the staff queue can filter and search by car without a join.
    car_brand = UnicodeAttribute(null=True)
    car_model_name = UnicodeAttribute(null=True)
    car_slug = UnicodeAttribute(null=True)
    # The car as a person would say it, snapshotted. Same instinct as
    # TestDriveBooking.car_label: a published thread outlives the car it is about, and
    # the email about it must still read correctly afterwards.
    car_label = UnicodeAttribute(null=True)

    customer_sub = UnicodeAttribute(null=True)
    customer_email = UnicodeAttribute(null=True)

    question = UnicodeAttribute(null=True)
    answer = UnicodeAttribute(null=True)

    answered_by = UnicodeAttribute(null=True)
    # This, not `answer != ""`, is what "has been answered" means -- fixing a typo months
    # later must not re-email the customer.
    #
    # It MUST stay genuinely unset when unanswered: the exactly-once answer email is a
    # conditional write on `answered_at.does_not_exist()`. Writing any placeholder here
    # silently destroys that guarantee. There is a test asserting exactly this.
    answered_at = UTCDateTimeAttribute(null=True)

    is_published = BooleanAttribute(default=False)
    language = UnicodeAttribute(default=QuestionLanguage.EN)

    search_blob = UnicodeAttribute(null=True)

    created_at = UTCDateTimeAttribute(null=True)
    updated_at = UTCDateTimeAttribute(null=True)

    def __str__(self):
        return f"Q{self.question_id}: {(self.question or '')[:40]}"

    @property
    def is_answered(self):
        return self.answered_at is not None

    @property
    def state(self):
        """What the shop still has to do about this one."""
        if not self.is_answered:
            return "Needs an answer"
        return "Published" if self.is_published else "Answered, not public"

    def build_search_blob(self):
        parts = [self.question, self.answer, self.car_brand, self.car_model_name,
                 self.customer_email]
        return " ".join(p for p in parts if p).casefold()


class Notification(BaseItem, discriminator="notif"):
    """One customer's bell.

    Still no stored text: the site has a language switcher, so a customer who signs up
    in English and later reads in Japanese must not find their history frozen. What is
    stored is the `kind` and enough `context` to render it at read time.
    """

    notification_id = UnicodeAttribute(null=True)
    customer_sub = UnicodeAttribute(null=True)
    kind = UnicodeAttribute(null=True)
    context = JSONAttribute(null=True)
    dedupe_key = UnicodeAttribute(null=True)
    read_at = UTCDateTimeAttribute(null=True)
    created_at = UTCDateTimeAttribute(null=True)

    # The trim-on-write dance the Django version needed is now mostly DynamoDB's job.
    ttl = NumberAttribute(null=True)

    def get_kind_display(self):
        return _display(NotificationKind, self.kind)


class DedupeGuard(BaseItem, discriminator="dedupe"):
    """Makes notify() idempotent: `one_notification_per_event`.

    Skipped entirely when `dedupe_key` is empty, which is both how the old partial
    constraint behaved and how we avoid DynamoDB's refusal of an empty key attribute.
    """

    notification_sk = UnicodeAttribute(null=True)
    ttl = NumberAttribute(null=True)


# --------------------------------------------------------------------------------------
# Uniqueness guards -- enforced by the primary key, against any writer
# --------------------------------------------------------------------------------------

class SlugGuard(BaseItem, discriminator="slug"):
    """`Car.slug` uniqueness *and* the slug -> car lookup, in one item.

    Because a slug is generated once and then left alone, this mapping is immutable in
    practice, which makes it safe to memoise for the life of a warm Lambda container.
    """

    car_id = UnicodeAttribute(null=True)


class ChassisGuard(BaseItem, discriminator="chassis"):
    car_id = UnicodeAttribute(null=True)


class RuleGuard(BaseItem, discriminator="rule"):
    schedule_id = UnicodeAttribute(null=True)


class LegacyCarPointer(BaseItem, discriminator="carid"):
    """Old numeric id -> slug, so `/cars/34` can still 301 after the migration."""

    slug = UnicodeAttribute(null=True)
    car_id = UnicodeAttribute(null=True)


# --------------------------------------------------------------------------------------
# Auth state
# --------------------------------------------------------------------------------------

class PendingRegistration(BaseItem, discriminator="pending"):
    """A sign-up waiting on its emailed link.

    Cognito holds the UNCONFIRMED user and the password from step one, so unlike the
    table this replaces there is no password hash here at all. What is left is the
    things Cognito has nowhere to put.
    """

    email = UnicodeAttribute(null=True)
    name = UnicodeAttribute(null=True)
    phone = UnicodeAttribute(null=True)
    next_path = UnicodeAttribute(null=True)
    language = UnicodeAttribute(default="en")
    token_hash = UnicodeAttribute(null=True)
    created_at = UTCDateTimeAttribute(null=True)
    ttl = NumberAttribute(null=True)


class PendingToken(BaseItem, discriminator="pendtok"):
    """Hash of the emailed link -> the address waiting on it."""

    email = UnicodeAttribute(null=True)
    ttl = NumberAttribute(null=True)


class ResetToken(BaseItem, discriminator="resettok"):
    """Hash of a password-reset link.

    Carries the epoch it was minted under so a completed reset can invalidate every
    sibling link, which is what deriving the token from the password hash used to do
    for free.
    """

    sub = UnicodeAttribute(null=True)
    email = UnicodeAttribute(null=True)
    epoch = NumberAttribute(default=0)
    ttl = NumberAttribute(null=True)


class ResetEpoch(BaseItem, discriminator="resetepoch"):
    n = NumberAttribute(default=0)


class LegacyPassword(BaseItem, discriminator="legacypw"):
    """A Django PBKDF2 hash, kept only until its owner next signs in.

    Cognito will not accept a hash on AdminCreateUser, so the alternative to this is
    emailing every customer to say their password no longer works. A UserMigration
    trigger verifies against this on first sign-in instead, and the customer notices
    nothing.
    """

    email = UnicodeAttribute(null=True)
    password_hash = UnicodeAttribute(null=True)
    name = UnicodeAttribute(null=True)
    phone = UnicodeAttribute(null=True)
    ttl = NumberAttribute(null=True)
