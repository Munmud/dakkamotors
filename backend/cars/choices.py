"""Enumerations, shared by everything that needs them.

Extracted during the DynamoDB migration so the ORM models and the store could not drift
apart while both existed. The ORM is gone; this is simply where the enums live now, read
by `cars/store/`, `cars/staff/forms.py` and the management commands.

`TextChoices` needs no database -- it is an enum with labels -- which is what makes it
safe to import from anywhere, including code running with `DATABASES = {}`.
"""

from django.db import models


class FuelType(models.TextChoices):
    PETROL = "petrol", "Petrol"
    # Beside Petrol rather than instead of it, at the owner's choice. The two are the
    # same fuel; existing cars say "petrol", and the shop wanted the word its customers
    # use available for new ones without a backfill.
    GASOLINE = "gasoline", "Gasoline"
    DIESEL = "diesel", "Diesel"
    HYBRID = "hybrid", "Hybrid"
    ELECTRIC = "electric", "Electric"
    LPG = "lpg", "LPG"


class CarStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    RESERVED = "reserved", "Reserved"
    SOLD = "sold", "Sold"


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Monday"
    TUESDAY = 1, "Tuesday"
    WEDNESDAY = 2, "Wednesday"
    THURSDAY = 3, "Thursday"
    FRIDAY = 4, "Friday"
    SATURDAY = 5, "Saturday"
    SUNDAY = 6, "Sunday"


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


class RequestStatus(models.TextChoices):
    """A car request is a lead. Open until somebody has dealt with it."""
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class QuestionLanguage(models.TextChoices):
    EN = "en", "English"
    JA = "ja", "日本語"


class NotificationKind(models.TextChoices):
    BOOKING_CONFIRMED = "booking_confirmed", "Test drive confirmed"
    BOOKING_CANCELLED = "booking_cancelled", "Test drive cancelled"
    QUESTION_ANSWERED = "question_answered", "Question answered"


#: Widths generated for every uploaded photo. 320 covers gallery thumbnails, 800 the
#: listing cards, 1600 the gallery's main image on a high-density screen.
DERIVATIVE_WIDTHS = (320, 800, 1600)
