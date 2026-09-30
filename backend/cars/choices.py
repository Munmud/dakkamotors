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


class RequestStatus(models.TextChoices):
    """A lead. Open until somebody has rung the person back and dealt with it."""

    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


#: The widths every uploaded photo is resized to, in WebP.
#:
#: Four rather than three. 320/800/1600 left an eight-hundred-pixel gap in the middle,
#: so a 390pt phone at 3x -- which is most of the traffic an advertisement brings --
#: was handed the 1600 and threw most of it away. 640 and 1000 cover the two common
#: phone widths; 1600 is the widest thing this site ever serves, and the original is
#: never handed to a browser at all.
DERIVATIVE_WIDTHS = (320, 640, 1000, 1600)
