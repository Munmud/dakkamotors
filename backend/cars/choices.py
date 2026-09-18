"""Enumerations shared by the ORM models and the DynamoDB store.

Extracted here during the DynamoDB migration so the two layers cannot drift apart while
both exist. `TextChoices` needs no database -- it is an enum with labels -- so this
module is safe to import from anywhere, including code that runs with `DATABASES = {}`.

Each definition keeps its original home's re-export, so existing imports such as
`from .models import FuelType` continue to work untouched.
"""

from django.db import models


class FuelType(models.TextChoices):
    PETROL = "petrol", "Petrol"
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
