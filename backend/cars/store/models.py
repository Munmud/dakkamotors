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

import posixpath

from django.core.files.storage import default_storage
from django.utils.text import slugify
from pynamodb.attributes import (
    BooleanAttribute,
    JSONAttribute,
    ListAttribute,
    NumberAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)

from ..choices import CarStatus, FuelType, RequestStatus
from .base import BaseItem


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
    # The name in Japanese, the way specs carry a `_ja` beside every `_en`. Optional:
    # a reader whose language has no value sees the English, which the client's
    # `pickLocalized` already does for specs. Slugs, JSON-LD, emails and the staff
    # pages keep the English name -- it is the identifier, this is the display.
    brand_ja = UnicodeAttribute(null=True)
    model_name_ja = UnicodeAttribute(null=True)
    model_code = UnicodeAttribute(null=True)
    chassis_number = UnicodeAttribute(null=True)
    manufacture_year = NumberAttribute(null=True)
    fuel_type = UnicodeAttribute(default=FuelType.PETROL)
    seat_capacity = NumberAttribute(default=5)
    color = UnicodeAttribute(null=True)
    color_ja = UnicodeAttribute(null=True)

    # Left unset for "call for price" -- deliberately distinct from a price of zero.
    # PynamoDB omits a null attribute entirely rather than writing {"NULL": true}, so
    # `attribute_not_exists(price_jpy)` really does mean "no price", and the existing
    # test that a blank price serialises as null rather than 0 still holds.
    price_jpy = NumberAttribute(null=True)

    status = UnicodeAttribute(default=CarStatus.AVAILABLE)

    # The day it sold, ISO, e.g. "2026-09-22". A date rather than a timestamp because
    # nobody knows the minute a car sold, and a string because an ISO date sorts as
    # one. It is what orders the "Recently sold" shelf: the shelf used to read
    # `created_at`, which is when a car was added to the site, so eleven cars imported
    # in one afternoon outranked the one that had actually just sold.
    sold_at = UnicodeAttribute(null=True)

    description_en = UnicodeAttribute(null=True)
    description_ja = UnicodeAttribute(null=True)

    slug = UnicodeAttribute(null=True)

    # Set only by `manage.py import_lot`, and read only by it: the source folder a
    # car was imported from, so a second run of an import that is twenty minutes of
    # photo uploads recognises what it already did rather than doubling the lot.
    import_key = UnicodeAttribute(null=True)

    # Free-form [{label_en, label_ja, value_en, value_ja}, ...], list order = display
    # order. A JSON blob on the car rather than child items, for the same reason
    # primary_image_ref is one: specs are always read with the car, never on their own
    # and never indexed, so a fourth bucket in cars.detail() would buy nothing. See
    # cars/specs.py for the validation, and for why the cap is twenty.
    specs = JSONAttribute(null=True)

    # Denormalised so the listing page is ONE query rather than one per card. Kept in
    # step by store.images.refresh_primary(); store/images.py lists every path that has
    # to call it. Miss one and a card goes stale.
    primary_image_ref = JSONAttribute(null=True)

    # Lowercased haystack for the staff substring search, written at save time. See
    # store/search.py for why the filtering happens in Python and not in a
    # FilterExpression.
    search_blob = UnicodeAttribute(null=True)

    # Advertising. The Meta ad set running for this one car, typed into the staff page
    # from Ads Manager, and the moment a booking stopped it. The point of the pair is
    # that the shop stops paying the minute the advertisement has done its job -- see
    # cars/advertising.py, and docs/ADS.md for where the id is found.
    #
    # `ad_paused_at` is also the claim: it is set by a conditional update, so of two
    # bookings landing in the same second exactly one pauses the ad set and emails the
    # owner about it.
    ad_set_id = UnicodeAttribute(null=True)
    ad_paused_at = UTCDateTimeAttribute(null=True)

    created_at = UTCDateTimeAttribute(null=True)
    updated_at = UTCDateTimeAttribute(null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populated by store.cars.detail(); not stored attributes.
        self.images = []
        self.videos = []
        self.media = []

    def __str__(self):
        return f"{self.manufacture_year} {self.brand} {self.model_name}".strip()

    @property
    def seo_title_plain(self):
        """"2008 Daihatsu Tanto" - the phrase a buyer would actually search for.

        No grade. It left the staff form, so new cars have none, and a title that
        silently changed shape depending on how old the record was would be worse than
        one that is simply year, make and model.
        """
        parts = [str(self.manufacture_year or ""), self.brand, self.model_name]
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
        """The haystack the staff search greps. Deliberately not including `specs`.

        This attribute is stored on the car item, which is projected whole into GSI1 and
        read by every listing page. Twenty staff-typed pairs in two languages would
        roughly double that read, on every page, to serve a search nobody has asked for.
        """
        parts = [self.brand, self.grade, self.model_name, self.model_code,
                 self.chassis_number, self.brand_ja, self.model_name_ja]
        return " ".join(p for p in parts if p).casefold()

    def name_in(self, language):
        """Year, make and model for a reader -- Japanese when the car has it."""
        if language == "ja":
            brand = self.brand_ja or self.brand
            model = self.model_name_ja or self.model_name
        else:
            brand, model = self.brand, self.model_name
        return " ".join(str(p) for p in (self.manufacture_year, brand, model) if p)

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


class CarVideo(BaseItem, discriminator="vid"):
    """A clip in the car's gallery.

    Its own item type rather than a `kind` attribute on `CarImage`, and the reason is
    `Car.primary_image_ref`. `images.for_car` queries `begins_with(sk, "IMG#")`, so the
    list `pick_primary` chooses from is homogeneous *by construction* and a video can
    never become the listing card photo. Sharing one item type would put a `kind` filter
    on all seven paths that maintain that reference, plus on `Car.primary_image` below,
    which rebuilds a detached `CarImage` from the ref and would otherwise happily
    construct one out of a video.

    Deliberately small. There are no derivatives -- no widths, no `IMG#PENDING` index
    entry, no `derivatives_ready` -- because nothing transcodes video here: the file is
    served as uploaded, and CloudFront handles byte-range seeking against S3 without
    help. `order` shares one number space with `CarImage.order`; `store/media.py` is the
    only definition of how the two interleave.
    """

    video_id = UnicodeAttribute(null=True)
    car_id = UnicodeAttribute(null=True)

    video_name = UnicodeAttribute(null=True)
    order = NumberAttribute(default=0)

    created_at = UTCDateTimeAttribute(null=True)

    def __str__(self):
        return f"Video {self.video_id}"

    @property
    def url(self):
        return default_storage.url(self.video_name) if self.video_name else ""


class SlugGuard(BaseItem, discriminator="slug"):
    """`Car.slug` uniqueness *and* the slug -> car lookup, in one item.

    Because a slug is generated once and then left alone, this mapping is immutable in
    practice, which makes it safe to memoise for the life of a warm Lambda container.
    """

    car_id = UnicodeAttribute(null=True)


class ChassisGuard(BaseItem, discriminator="chassis"):
    car_id = UnicodeAttribute(null=True)


class LegacyCarPointer(BaseItem, discriminator="carid"):
    """Old numeric id -> slug, so `/cars/34` can still 301 after the migration."""

    slug = UnicodeAttribute(null=True)
    car_id = UnicodeAttribute(null=True)


class CarRequest(BaseItem, discriminator="req"):
    """Somebody telling the shop they are interested. The only thing the site collects.

    Two cases, one item. **With a car** it is "I want this one, call me" -- the whole
    job of a car page. **Without** it is "I am looking for something you do not have",
    which is the same person one step earlier. They differ by one optional field, so
    they share a queue, an email and a set of rules rather than being two features that
    drift apart.

    Nothing here points at an account, because there are none: a name and a number is
    what the shop needs to ring somebody back, and the email is a convenience on top.
    No TTL -- the owner reuses these for advertising, and a lead is worth keeping until
    somebody decides otherwise.
    """

    request_id = UnicodeAttribute(null=True)
    name = UnicodeAttribute(null=True)
    email = UnicodeAttribute(null=True)
    phone = UnicodeAttribute(null=True)
    details = UnicodeAttribute(null=True)
    language = UnicodeAttribute(default="en")

    # The car this lead is about, or nothing. Denormalised, because a lead outlives the
    # car: a sold one is deleted and takes its photos with it, and staff reading the
    # queue later still need to know what was asked about. Absent on a "find me
    # something" lead, which is the same person one step earlier.
    car_id = UnicodeAttribute(null=True)
    car_slug = UnicodeAttribute(null=True)
    car_label = UnicodeAttribute(null=True)
    status = UnicodeAttribute(default=RequestStatus.OPEN)
    resolved_at = UTCDateTimeAttribute(null=True)
    resolved_by = UnicodeAttribute(null=True)
    resolution_note = UnicodeAttribute(null=True)
    created_at = UTCDateTimeAttribute(null=True)
    updated_at = UTCDateTimeAttribute(null=True)

    def __str__(self):
        return f"{self.name} - {(self.details or '')[:40]}"

    def get_status_display(self):
        return _display(RequestStatus, self.status)

    @property
    def is_open(self):
        return self.status == RequestStatus.OPEN
